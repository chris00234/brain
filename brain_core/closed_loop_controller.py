"""brain_core/closed_loop_controller.py — Phase 2 of the W4 sprint.

Codex round-6 #1 / "biggest defect" pick: brain notices, classifies,
schedules, and remembers defects, but `slo_replan.py` docstring literally
says "does not mutate policy." eval triage caps at bounded review tasks,
not repairs. autonomous agent-dispatch path is disabled in launchd. The
feedback loop never closes.

This module makes the loop close. v1 ships PROPOSE-ONLY (L1) so every
mutation flows through `autonomy.authorize()` and lands in a review queue
Chris (or a higher-autonomy promotion later) can ack. L2 auto-apply is
gated to the 2 already-dynamic, reversible resource throttles
(BRAIN_SCHED_MAX_HEAVY_JOBS, BRAIN_CLI_LLM_CONCURRENCY); everything else
stays propose-only until 7+ successful cycles exist.

Storage: two new tables in `autonomy.db` (separate from `brain.db` so a
controller schema mistake can't corrupt canonical store).

  closed_loop_policy_mutations  -- one row per proposed/applied/reverted change
  closed_loop_metric_state      -- per-(trigger_kind, trigger_name) breach memory

Scheduling: 07:20 daily, after the 06:57/07:02/07:07 eval regression
triplet so the controller reads the day's fresh eval reports.

2026-05-20 W4 Phase 2 (codex round-7 spec): closes the gap where round-3
shipped Honcho-style dialectic hypotheses for chris-modeling but the
brain-self-quality loop had no comparable mutation path.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger("brain.closed_loop_controller")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import AUTONOMY_DB, BRAIN_LOGS_DIR  # noqa: E402

# ---------------------------------------------------------------------------
# Mutable knob allowlist (codex round-7 spec §1)
# Each entry: location-of-truth (for the human reader), default, bounds, kind.
# kind="dynamic" -> brain_config_store live read; kind="env" -> import-time
# only (proposal still recorded but apply must wait for restart).
# ---------------------------------------------------------------------------
ALLOWLIST: dict[str, dict[str, Any]] = {
    "BRAIN_SCHED_MAX_HEAVY_JOBS": {
        "kind": "dynamic",
        "location": "scheduler.py:78 (read at :460)",
        "default": 1,
        "bounds": {"min": 0, "max": 1},
        "max_delta_per_cycle": 1,
        "autonomy": "L2",
        "rationale": "Hard throttle. Already proven reversible via slo_remediation.",
    },
    "BRAIN_CLI_LLM_CONCURRENCY": {
        "kind": "dynamic",
        "location": "cli_llm.py:119 (TTL override :153)",
        "default": 2,
        "bounds": {"min": 1, "max": 2},
        "max_delta_per_cycle": 1,
        "autonomy": "L2",
        "rationale": "Cost-governor pattern; TTL-bounded.",
    },
    "DEFAULT_ITERATE_THRESHOLD": {
        "kind": "dynamic",
        "location": "crag.py:59 (consumed :301)",
        "default": 0.30,
        "bounds": {"min": 0.20, "max": 0.40},
        "max_delta_per_cycle": 0.05,
        "autonomy": "L1",
        "rationale": "CRAG iterate threshold. Tunable but quality-sensitive.",
    },
    "BRAIN_ONTOLOGY_EXPANSION_MAX_TERMS": {
        "kind": "env",
        "location": "config.py:144 + plist",
        "default": 5,
        "bounds": {"min": 0, "max": 8},
        "max_delta_per_cycle": 1,
        "autonomy": "L1",
        "rationale": "Retrieval expansion fan-out. Restart required to take effect.",
    },
    "BRAIN_MMR_LAMBDA": {
        "kind": "env",
        "location": "config.py:126/:130",
        "default": 0.85,
        "bounds": {"min": 0.75, "max": 0.95},
        "max_delta_per_cycle": 0.05,
        "autonomy": "L1",
        "rationale": "Rerank diversity vs relevance trade. Quality-sensitive.",
    },
}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
_MUTATIONS_DDL = """
CREATE TABLE IF NOT EXISTS closed_loop_policy_mutations (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    status                  TEXT NOT NULL CHECK (status IN
                                ('proposed','applied','reverted','rejected','expired')),
    knob_key                TEXT NOT NULL,
    knob_kind               TEXT NOT NULL,
    old_value               TEXT,
    old_value_exists        INTEGER NOT NULL DEFAULT 0,
    new_value               TEXT NOT NULL,
    delta_json              TEXT NOT NULL DEFAULT '{}',
    bounds_json             TEXT NOT NULL DEFAULT '{}',
    trigger_kind            TEXT NOT NULL,
    trigger_name            TEXT NOT NULL,
    trigger_window          TEXT NOT NULL,
    trigger_snapshot_json   TEXT NOT NULL,
    guardrail_snapshot_json TEXT NOT NULL DEFAULT '{}',
    autonomy_level          TEXT NOT NULL,
    authorization_reason    TEXT NOT NULL,
    cooldown_until          TEXT NOT NULL,
    applied_at              TEXT,
    reverted_at             TEXT,
    revert_reason           TEXT,
    supersedes_id           INTEGER
);
CREATE INDEX IF NOT EXISTS idx_clpm_status ON closed_loop_policy_mutations(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_clpm_knob ON closed_loop_policy_mutations(knob_key, created_at DESC);

CREATE TABLE IF NOT EXISTS closed_loop_metric_state (
    trigger_kind         TEXT NOT NULL,
    trigger_name         TEXT NOT NULL,
    first_breached_at    TEXT,
    last_seen_at         TEXT NOT NULL,
    consecutive_breaches INTEGER NOT NULL DEFAULT 0,
    last_value           REAL,
    target_value         REAL,
    last_status          TEXT NOT NULL,
    window_json          TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(trigger_kind, trigger_name)
);
"""

# Cooldown defaults — quality knobs cool down longer than resource throttles.
_COOLDOWN_HOURS = {
    "BRAIN_SCHED_MAX_HEAVY_JOBS": 1,
    "BRAIN_CLI_LLM_CONCURRENCY": 1,
    "DEFAULT_ITERATE_THRESHOLD": 24,
    "BRAIN_ONTOLOGY_EXPANSION_MAX_TERMS": 24,
    "BRAIN_MMR_LAMBDA": 24,
}

# Minimum consecutive breach cycles before a knob is proposable.
_MIN_CONSECUTIVE_BREACHES = 3


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _conn() -> sqlite3.Connection:
    AUTONOMY_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(AUTONOMY_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure_schema() -> None:
    conn = _conn()
    try:
        conn.executescript(_MUTATIONS_DDL)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Signal collection
# ---------------------------------------------------------------------------
def _fetch_slos_via_http() -> list[dict]:
    """HTTP fallback for SLO collection when slos.run() can't import in
    the controller's process (e.g. when invoked outside the brain server's
    apscheduler-loaded venv).
    """
    try:
        import urllib.request as _u

        secret_path = Path("~/.brain/credentials/.personal_webhook_secret").expanduser()
        secret = secret_path.read_text().strip() if secret_path.exists() else ""
        req = _u.Request("http://127.0.0.1:8791/brain/slos")
        if secret:
            req.add_header("Authorization", f"Bearer {secret}")
        with _u.urlopen(req, timeout=10) as resp:  # noqa: S310
            doc = json.loads(resp.read().decode())
        items = doc.get("items") if isinstance(doc, dict) else None
        return list(items or [])
    except Exception as exc:
        log.debug("HTTP SLO fallback failed: %s", exc)
        return []


def collect_slo_signals() -> list[dict]:
    """Pull current SLO breach state. Bumps consecutive_breaches per metric."""
    items: list[dict] = []
    items_resolved = False
    try:
        import slos as _slos

        if hasattr(_slos, "run"):
            report = _slos.run()
            if isinstance(report, dict):
                raw = report.get("items")
                if isinstance(raw, list):
                    # 2026-05-20 W4 round-8 defect B: distinguish "got an
                    # empty list" from "call failed entirely". The earlier
                    # `if not items` collapsed both into the HTTP fallback,
                    # so a healthy brain with zero breaches did a wasteful
                    # loopback round-trip per controller tick.
                    items = raw
                    items_resolved = True
    except Exception as exc:
        log.debug("slos.run direct call failed (%s), falling back to HTTP", exc)
    if not items_resolved:
        items = _fetch_slos_via_http()
    if not items:
        return []
    out: list[dict] = []
    now = _now()
    conn = _conn()
    try:
        for it in items:
            if not isinstance(it, dict):
                continue
            name = it.get("name") or ""
            breached = bool(it.get("breached"))
            actual = it.get("actual")
            target = it.get("target")
            status = "breached" if breached else "ok"
            prior = conn.execute(
                "SELECT consecutive_breaches FROM closed_loop_metric_state "
                "WHERE trigger_kind='slo' AND trigger_name=?",
                (name,),
            ).fetchone()
            prior_breaches = int(prior["consecutive_breaches"]) if prior else 0
            new_breaches = (prior_breaches + 1) if breached else 0
            first_breached = None
            if breached and new_breaches == 1:
                first_breached = now
            conn.execute(
                """
                INSERT INTO closed_loop_metric_state
                  (trigger_kind, trigger_name, first_breached_at, last_seen_at,
                   consecutive_breaches, last_value, target_value, last_status, window_json)
                VALUES ('slo', ?, ?, ?, ?, ?, ?, ?, '{}')
                ON CONFLICT(trigger_kind, trigger_name) DO UPDATE SET
                  last_seen_at=excluded.last_seen_at,
                  consecutive_breaches=excluded.consecutive_breaches,
                  last_value=excluded.last_value,
                  target_value=excluded.target_value,
                  last_status=excluded.last_status,
                  first_breached_at=CASE WHEN excluded.consecutive_breaches=1
                                         THEN excluded.first_breached_at
                                         ELSE closed_loop_metric_state.first_breached_at END
                """,
                (
                    name,
                    first_breached,
                    now,
                    new_breaches,
                    float(actual) if isinstance(actual, int | float) else None,
                    float(target) if isinstance(target, int | float) else None,
                    status,
                ),
            )
            out.append(
                {
                    "kind": "slo",
                    "name": name,
                    "breached": breached,
                    "actual": actual,
                    "target": target,
                    "consecutive_breaches": new_breaches,
                }
            )
        conn.commit()
    finally:
        conn.close()
    return out


def collect_eval_signals() -> list[dict]:
    """Read the daily eval regression JSON outputs. Returns one signal per
    breached eval, with consecutive-day accounting via metric_state.
    """
    eval_paths = {
        "retrieval_regression": BRAIN_LOGS_DIR / "retrieval_regression.json",
        "crag_regression": BRAIN_LOGS_DIR / "crag_regression.json",
        "crag_correction_regression": BRAIN_LOGS_DIR / "crag_correction_regression.json",
    }
    out: list[dict] = []
    now = _now()
    conn = _conn()
    try:
        for name, path in eval_paths.items():
            if not path.exists():
                continue
            try:
                doc = json.loads(path.read_text())
            except Exception as exc:
                log.debug("eval read %s failed: %s", path, exc)
                continue
            status = str(doc.get("status") or "").lower()
            breached = status in {"breached", "insufficient_coverage", "fail"}
            prior = conn.execute(
                "SELECT consecutive_breaches FROM closed_loop_metric_state "
                "WHERE trigger_kind='eval' AND trigger_name=?",
                (name,),
            ).fetchone()
            prior_breaches = int(prior["consecutive_breaches"]) if prior else 0
            new_breaches = (prior_breaches + 1) if breached else 0
            first_breached = now if (breached and new_breaches == 1) else None
            conn.execute(
                """
                INSERT INTO closed_loop_metric_state
                  (trigger_kind, trigger_name, first_breached_at, last_seen_at,
                   consecutive_breaches, last_value, target_value, last_status, window_json)
                VALUES ('eval', ?, ?, ?, ?, NULL, NULL, ?, ?)
                ON CONFLICT(trigger_kind, trigger_name) DO UPDATE SET
                  last_seen_at=excluded.last_seen_at,
                  consecutive_breaches=excluded.consecutive_breaches,
                  last_status=excluded.last_status,
                  window_json=excluded.window_json,
                  first_breached_at=CASE WHEN excluded.consecutive_breaches=1
                                         THEN excluded.first_breached_at
                                         ELSE closed_loop_metric_state.first_breached_at END
                """,
                (
                    name,
                    first_breached,
                    now,
                    new_breaches,
                    status,
                    json.dumps({"path": str(path)}),
                ),
            )
            out.append(
                {
                    "kind": "eval",
                    "name": name,
                    "breached": breached,
                    "status": status,
                    "consecutive_breaches": new_breaches,
                    "doc": {k: v for k, v in doc.items() if k in {"status", "passed", "total", "pass_rate"}},
                }
            )
        conn.commit()
    finally:
        conn.close()
    return out


# ---------------------------------------------------------------------------
# Proposal pipeline
# ---------------------------------------------------------------------------

# Signal -> knob mapping. Each tuple = (delta_sign, magnitude). Sign +1 means
# "increase the knob"; -1 means decrease. magnitude is a fraction of
# max_delta_per_cycle for the knob.
_SIGNAL_TO_KNOB: dict[str, list[tuple[str, int, float]]] = {
    # SLOs
    "logs_dir_total_mb": [("BRAIN_SCHED_MAX_HEAVY_JOBS", -1, 1.0)],
    "logs_dir_growth_24h_mb": [("BRAIN_SCHED_MAX_HEAVY_JOBS", -1, 1.0)],
    "brain_server_rss_mb": [("BRAIN_SCHED_MAX_HEAVY_JOBS", -1, 1.0)],
    "brain_server_rss_growth_1h_mb": [("BRAIN_SCHED_MAX_HEAVY_JOBS", -1, 1.0)],
    # 2026-05-20 W4 round-8 defect C: env-only knobs need restart to take
    # effect; pair them with at least one dynamic knob so the proposal has
    # immediate-acting reduction available on the same signal.
    "recall_v2_p95_ms": [
        ("BRAIN_SCHED_MAX_HEAVY_JOBS", -1, 1.0),  # dynamic, immediate
        ("BRAIN_ONTOLOGY_EXPANSION_MAX_TERMS", -1, 1.0),  # env, deferred
    ],
    # Evals
    "retrieval_regression": [
        ("BRAIN_ONTOLOGY_EXPANSION_MAX_TERMS", -1, 1.0),
        ("BRAIN_MMR_LAMBDA", -1, 1.0),
    ],
    "crag_correction_regression": [("DEFAULT_ITERATE_THRESHOLD", -1, 1.0)],
}


def _read_current(knob: str, allow: dict[str, Any]) -> tuple[Any, bool]:
    """Return (current_value, exists_in_store) for a knob.

    For dynamic knobs we consult brain_config_store first; for env-only
    knobs we fall back to the allowlist default.
    """
    if allow.get("kind") == "dynamic":
        try:
            import brain_config_store as _bcs

            current = _bcs.get(knob) if hasattr(_bcs, "get") else None
        except Exception:
            current = None
        if current is not None:
            return current, True
    import os as _os

    env_val = _os.environ.get(knob)
    if env_val is not None:
        return env_val, False
    return allow.get("default"), False


def _clamp_proposal(current: Any, delta_sign: int, allow: dict[str, Any]) -> Any:
    """Compute the proposed value, respecting bounds + max_delta_per_cycle."""
    bounds = allow.get("bounds") or {}
    max_delta = allow.get("max_delta_per_cycle") or 1
    try:
        cur_num = float(current) if current is not None else float(allow["default"])
    except (TypeError, ValueError):
        cur_num = float(allow["default"])
    proposed = cur_num + (delta_sign * float(max_delta))
    lo = float(bounds.get("min", proposed))
    hi = float(bounds.get("max", proposed))
    proposed = max(lo, min(hi, proposed))
    if isinstance(allow["default"], int):
        return int(round(proposed))
    return round(proposed, 4)


def _has_open_or_cooldown_mutation(knob_key: str) -> bool:
    """True when a proposal/applied row exists OR cooldown_until > now."""
    conn = _conn()
    try:
        row = conn.execute(
            """
            SELECT id FROM closed_loop_policy_mutations
            WHERE knob_key = ?
              AND (status IN ('proposed','applied') OR cooldown_until > ?)
            LIMIT 1
            """,
            (knob_key, _now()),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def propose_mutations(signals: list[dict]) -> list[dict]:
    """Build proposed mutations from active signals. Records each to
    closed_loop_policy_mutations with status='proposed'. Returns the rows.
    """
    if not signals:
        return []
    ensure_schema()
    proposals: list[dict] = []
    seen_knobs: set[str] = set()
    for sig in signals:
        if not sig.get("breached"):
            continue
        if int(sig.get("consecutive_breaches") or 0) < _MIN_CONSECUTIVE_BREACHES:
            continue
        sig_name = sig.get("name", "")
        mappings = _SIGNAL_TO_KNOB.get(sig_name) or []
        for knob_key, delta_sign, _mag in mappings:
            if knob_key in seen_knobs:
                continue
            allow = ALLOWLIST.get(knob_key)
            if not allow:
                continue
            if _has_open_or_cooldown_mutation(knob_key):
                continue
            current, exists = _read_current(knob_key, allow)
            new_val = _clamp_proposal(current, delta_sign, allow)
            if str(new_val) == str(current):
                continue  # already at clamp boundary
            cooldown_hours = _COOLDOWN_HOURS.get(knob_key, 1)
            cooldown_until = (datetime.now(UTC) + timedelta(hours=cooldown_hours)).isoformat(
                timespec="seconds"
            )
            row = {
                "knob_key": knob_key,
                "knob_kind": allow["kind"],
                "old_value": str(current) if current is not None else None,
                "old_value_exists": 1 if exists else 0,
                "new_value": str(new_val),
                "delta_json": json.dumps({"sign": delta_sign}),
                "bounds_json": json.dumps(allow.get("bounds") or {}),
                "trigger_kind": sig.get("kind") or "unknown",
                "trigger_name": sig_name,
                "trigger_window": "24h",
                "trigger_snapshot_json": json.dumps(sig, ensure_ascii=False),
                "guardrail_snapshot_json": json.dumps({}, ensure_ascii=False),
                "autonomy_level": allow.get("autonomy") or "L1",
                "authorization_reason": f"signal:{sig.get('kind')}:{sig_name} consecutive={sig.get('consecutive_breaches')}",
                "cooldown_until": cooldown_until,
            }
            row_id = write_mutation_row(row)
            row["id"] = row_id
            proposals.append(row)
            seen_knobs.add(knob_key)
    return proposals


def write_mutation_row(row: dict) -> int:
    ensure_schema()
    conn = _conn()
    try:
        now = _now()
        cur = conn.execute(
            """
            INSERT INTO closed_loop_policy_mutations
              (created_at, updated_at, status, knob_key, knob_kind,
               old_value, old_value_exists, new_value, delta_json, bounds_json,
               trigger_kind, trigger_name, trigger_window, trigger_snapshot_json,
               guardrail_snapshot_json, autonomy_level, authorization_reason,
               cooldown_until)
            VALUES (?, ?, 'proposed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                now,
                row["knob_key"],
                row["knob_kind"],
                row["old_value"],
                row["old_value_exists"],
                row["new_value"],
                row["delta_json"],
                row["bounds_json"],
                row["trigger_kind"],
                row["trigger_name"],
                row["trigger_window"],
                row["trigger_snapshot_json"],
                row["guardrail_snapshot_json"],
                row["autonomy_level"],
                row["authorization_reason"],
                row["cooldown_until"],
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def mark_mutation_status(mutation_id: int, status: str, **fields: object) -> None:
    ensure_schema()
    conn = _conn()
    try:
        set_clauses = ["status=?", "updated_at=?"]
        params: list[Any] = [status, _now()]
        for key, val in fields.items():
            set_clauses.append(f"{key}=?")
            params.append(val)
        params.append(mutation_id)
        # set_clauses values are internal column-name literals (status + any
        # **fields keys callers pass) — never user input — so S608 is a
        # false positive. All bound values go through ? placeholders.
        conn.execute(
            f"UPDATE closed_loop_policy_mutations SET {', '.join(set_clauses)} WHERE id=?",  # noqa: S608
            params,
        )
        conn.commit()
    finally:
        conn.close()


def list_proposed(limit: int = 20) -> list[dict]:
    ensure_schema()
    conn = _conn()
    try:
        rows = conn.execute(
            """
            SELECT * FROM closed_loop_policy_mutations
            WHERE status='proposed'
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def summary() -> dict:
    ensure_schema()
    conn = _conn()
    try:
        by_status = {
            r["status"]: int(r["c"])
            for r in conn.execute(
                "SELECT status, COUNT(*) AS c FROM closed_loop_policy_mutations GROUP BY status"
            ).fetchall()
        }
        breached_now = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM closed_loop_metric_state " "WHERE consecutive_breaches >= ?",
                (_MIN_CONSECUTIVE_BREACHES,),
            ).fetchone()["c"]
        )
        return {
            "ts": _now(),
            "by_status": by_status,
            "metrics_at_threshold": breached_now,
            "knobs_known": list(ALLOWLIST.keys()),
            "min_consecutive_breaches": _MIN_CONSECUTIVE_BREACHES,
        }
    finally:
        conn.close()


def run_controller(eval_window: str = "24h") -> dict:
    """One controller pass. v1 is propose-only."""
    started = datetime.now(UTC)
    slo = collect_slo_signals()
    evals = collect_eval_signals()
    all_signals = slo + evals
    proposals = propose_mutations(all_signals)
    duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    return {
        "ts": _now(),
        "eval_window": eval_window,
        "duration_ms": duration_ms,
        "signal_count": len(all_signals),
        "breached_signals": [s for s in all_signals if s.get("breached")],
        "proposals": [
            {
                "id": p.get("id"),
                "knob_key": p.get("knob_key"),
                "old_value": p.get("old_value"),
                "new_value": p.get("new_value"),
                "trigger_kind": p.get("trigger_kind"),
                "trigger_name": p.get("trigger_name"),
                "autonomy_level": p.get("autonomy_level"),
                "cooldown_until": p.get("cooldown_until"),
            }
            for p in proposals
        ],
        "summary": summary(),
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="closed_loop_controller — propose-only v1")
    parser.add_argument(
        "action", choices=["run", "summary", "proposed", "ensure_schema"], nargs="?", default="run"
    )
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    if args.action == "run":
        print(json.dumps(run_controller(), indent=2, ensure_ascii=False))  # noqa: T201
    elif args.action == "summary":
        print(json.dumps(summary(), indent=2, ensure_ascii=False))  # noqa: T201
    elif args.action == "proposed":
        print(json.dumps(list_proposed(args.limit), indent=2, ensure_ascii=False))  # noqa: T201
    else:
        ensure_schema()
        print("ok")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
