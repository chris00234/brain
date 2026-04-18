"""brain_core/autonomy_proposer.py - Phase 7 autonomy auto-tune.

Aggregates action outcomes from `action_audit` (brain.db) grouped by `tool`
(the autonomy `kind` namespace: heal.reindex, task.dispatch, brain_loop.*).
For each kind with sufficient sample size and high success_ratio, proposes
an autonomy level upgrade (L1->L2, L2->L3) by appending an event to
audit_log for human review.

NEVER auto-applies. Always surfaces as an audit review item.

Mirror logic: if the success ratio falls below DEMOTE_RATIO, propose a
downgrade (L3 -> L2, L2 -> L1) and tick the breaker for that kind.

2026-04-16 fix: previously read from `accuracy_tracker.domain`, whose values
are topic buckets ("infra", "coding", "personal", "general") that never
overlap with autonomy kinds. levels.get(kind) always returned the default
"L1" and current_level == "L2" never matched — the proposer had been a
no-op since ship. Now sourced from action_audit.tool which DOES share the
autonomy kind namespace. Promotion path also extended L1->L2 (previously
only L2->L3 was wired, stranding every L1 kind forever).
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

log = logging.getLogger("brain.autonomy_proposer")

try:
    from config import AUTONOMY_DB
except ImportError:
    AUTONOMY_DB = Path("/Users/chrischo/server/brain/logs/autonomy.db")

try:
    from atoms_store import BRAIN_DB
except ImportError:
    BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")


MIN_OUTCOMES_FOR_PROMOTE = 20
PROMOTE_RATIO = 0.95
DEMOTE_RATIO = 0.60
WINDOW_DAYS = 30  # only look at recent track record
SUCCESS_OUTCOMES = {"success", "ok", "approved", "completed", "committed"}
FAILURE_OUTCOMES = {"fail", "error", "rejected", "rollback", "timeout", "denied"}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(AUTONOMY_DB))
    conn.row_factory = sqlite3.Row
    return conn


def _propose_audit(kind: str, current_level: str, target_level: str, reason: str) -> None:
    try:
        from audit_log import log_event

        log_event(
            event_type="autonomy_proposal",
            entity_a=kind,
            entity_b=f"{current_level}->{target_level}",
            resolution="proposed",
            reason=reason,
            review_required=True,
        )
    except Exception as exc:
        log.warning("audit log write failed: %s", exc)


def _fetch_kind_outcomes() -> list[dict]:
    """Aggregate action_audit by tool over the last WINDOW_DAYS.

    Returns [{"kind": tool, "total": N, "success": S, "failure": F}, ...]
    Only rows whose outcome is in SUCCESS_OUTCOMES ∪ FAILURE_OUTCOMES are
    counted. Rows with NULL outcome (still pending) are skipped.
    """
    if not BRAIN_DB.exists():
        return []
    try:
        conn = sqlite3.connect(str(BRAIN_DB))
        conn.row_factory = sqlite3.Row
        try:
            cutoff = datetime.now(UTC).timestamp() - WINDOW_DAYS * 86400
            cutoff_iso = datetime.fromtimestamp(cutoff, UTC).isoformat(timespec="seconds")
            rows = conn.execute(
                "SELECT tool, outcome, COUNT(*) AS n "
                "FROM action_audit "
                "WHERE created_at >= ? AND tool IS NOT NULL AND tool != '' "
                "AND outcome IS NOT NULL "
                "GROUP BY tool, outcome",
                (cutoff_iso,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []

    agg: dict[str, dict] = {}
    for r in rows:
        kind = r["tool"]
        outcome = (r["outcome"] or "").strip().lower()
        n = int(r["n"] or 0)
        slot = agg.setdefault(kind, {"kind": kind, "total": 0, "success": 0, "failure": 0})
        if outcome in SUCCESS_OUTCOMES:
            slot["success"] += n
            slot["total"] += n
        elif outcome in FAILURE_OUTCOMES:
            slot["failure"] += n
            slot["total"] += n
        # other outcomes (neutral/ignored) not counted
    return list(agg.values())


def run() -> dict:
    """Walk action_audit by kind, propose level changes, write audit_log entries.

    Returns a dict summary for the scheduler.
    """
    try:
        from autonomy import list_levels
    except Exception:
        list_levels = lambda: {}  # noqa: E731

    levels = list_levels()
    promotes: list[dict] = []
    demotes: list[dict] = []
    skipped: list[str] = []

    kind_rows = _fetch_kind_outcomes()
    if not kind_rows:
        return {
            "promoted_proposals": 0,
            "demoted_proposals": 0,
            "skipped": 0,
            "note": "no_action_audit_window_data",
        }

    for row in kind_rows:
        kind = row["kind"]
        total = int(row["total"] or 0)
        success = int(row["success"] or 0)
        if total < MIN_OUTCOMES_FOR_PROMOTE:
            continue
        ratio = success / total
        current_level = levels.get(kind, "L1")

        # Promote path — now covers L1->L2 AND L2->L3 (was L2->L3 only).
        if ratio >= PROMOTE_RATIO and current_level in ("L1", "L2"):
            target = "L2" if current_level == "L1" else "L3"
            _propose_audit(
                kind,
                current_level,
                target,
                f"success_ratio={ratio:.3f} ({success}/{total}) >= {PROMOTE_RATIO}",
            )
            promotes.append({"kind": kind, "ratio": ratio, "total": total, "target": target})
        elif ratio <= DEMOTE_RATIO and current_level in ("L2", "L3"):
            target = "L2" if current_level == "L3" else "L1"
            _propose_audit(
                kind,
                current_level,
                target,
                f"success_ratio={ratio:.3f} ({success}/{total}) <= {DEMOTE_RATIO}",
            )
            demotes.append({"kind": kind, "ratio": ratio, "total": total, "target": target})
            # Tick the breaker so subsequent actions get short-circuited
            try:
                from breakers import record_result

                record_result(kind, ok=False, error="autonomy_proposer:low_accuracy")
            except Exception as exc:
                log.warning("breaker tick failed for %s: %s", kind, exc)
        else:
            skipped.append(kind)

    return {
        "promoted_proposals": len(promotes),
        "demoted_proposals": len(demotes),
        "skipped": len(skipped),
        "promotes": promotes,
        "demotes": demotes,
        "window_days": WINDOW_DAYS,
    }


if __name__ == "__main__":
    import json
    import sys as _sys

    _sys.stdout.write(json.dumps(run(), indent=2) + "\n")
