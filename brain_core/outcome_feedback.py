"""brain_core/outcome_feedback.py — turn task_queue.outcomes overrides into
reviewable patterns.

Decision_ledger only captures decisions that brain_loop itself recorded.
The outcomes table holds every recorded task outcome including chris
overrides, which is where the heavy override traffic (~250+ rows/30d in
infra and coding combined) actually lives. With nothing reading that
signal back into a review queue the per-domain agency level stays
'frozen' indefinitely — there is no path from "Chris keeps overriding"
to "investigate why" beyond an operator manually scanning the table.

This module mirrors `decision_ledger.decision_feedback_report` /
`create_feedback_review_tasks` against the outcomes table:

  * `override_patterns_report` groups recent overrides by
    (domain, override_reason) and ranks them by an explicit severity score.
  * `create_override_review_tasks` creates bounded review tasks for the
    top patterns. Stable signatures prevent duplicates across runs.

Both functions are read-only with respect to policy and never call an
LLM — they only produce structured candidates and create review tasks
that route to a human or an investigation agent.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger("brain.outcome_feedback")


try:
    from brain_core.config import AUTONOMY_DB
except ImportError:  # pragma: no cover - direct execution fallback
    try:
        from config import AUTONOMY_DB
    except ImportError:
        AUTONOMY_DB = Path("/Users/chrischo/server/brain/logs/autonomy.db")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _cutoff_iso(hours: int) -> str:
    return (datetime.now(UTC) - timedelta(hours=max(1, int(hours)))).strftime("%Y-%m-%dT%H:%M:%S+00:00")


@contextmanager
def _conn(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    path = Path(db_path or AUTONOMY_DB)
    if not path.exists():
        yield None  # type: ignore[misc]
        return
    conn = sqlite3.connect(str(path), timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def override_patterns_report(
    *,
    hours: int = 168,
    min_overrides: int = 2,
    limit: int = 500,
    db_path: Path | str | None = None,
) -> dict:
    """Group recent chris_override outcomes by (domain, override_reason).

    A pattern surfaces when at least `min_overrides` separate task outcomes
    share the same domain + override reason, meaning the brain's
    recommendation kept missing the same way. The severity score weighs
    raw count, override rate within the domain, and recency.

    Read-only: never mutates outcomes, accuracy_tracker, or policy.
    Deterministic: no LLM call, no embedding load.
    """
    cutoff = _cutoff_iso(hours)
    capped_limit = max(1, min(int(limit or 500), 5000))

    with _conn(db_path) as conn:
        if conn is None:
            return _empty_report(hours, reason="autonomy_db_missing")
        outcomes = conn.execute(
            """
            SELECT id, task_id, domain, brain_recommendation, actual_action,
                   chris_override, override_reason, confidence_was, created_at
            FROM outcomes
            WHERE created_at >= ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (cutoff, capped_limit),
        ).fetchall()
        domain_totals: dict[str, int] = {}
        for row in conn.execute(
            "SELECT domain, COUNT(*) AS n FROM outcomes WHERE created_at >= ? GROUP BY domain",
            (cutoff,),
        ).fetchall():
            domain_totals[str(row["domain"] or "general")] = int(row["n"] or 0)

    groups: dict[tuple[str, str], dict] = {}
    sampled = 0
    for row in outcomes:
        sampled += 1
        if not int(row["chris_override"] or 0):
            continue
        domain = str(row["domain"] or "general")
        reason = _normalize_reason(row["override_reason"])
        key = (domain, reason)
        group = groups.setdefault(
            key,
            {
                "domain": domain,
                "override_reason": reason,
                "overrides": 0,
                "confidence_sum": 0.0,
                "sample_task_ids": [],
                "sample_actions": [],
                "last_seen": "",
                "first_seen": row["created_at"],
            },
        )
        group["overrides"] += 1
        group["confidence_sum"] += _float(row["confidence_was"])
        _append_sample(group["sample_task_ids"], str(row["task_id"] or ""))
        _append_sample(group["sample_actions"], str(row["actual_action"] or ""))
        if not group["last_seen"] or row["created_at"] > group["last_seen"]:
            group["last_seen"] = row["created_at"]
        if row["created_at"] < group["first_seen"]:
            group["first_seen"] = row["created_at"]

    candidates: list[dict] = []
    min_required = max(1, int(min_overrides or 2))
    for (domain, reason), group in groups.items():
        if group["overrides"] < min_required:
            continue
        domain_total = max(1, domain_totals.get(domain, group["overrides"]))
        override_rate = round(group["overrides"] / domain_total, 4)
        avg_confidence = round(group["confidence_sum"] / group["overrides"], 3) if group["overrides"] else 0.0
        # Severity ranks raw count (capped), domain saturation, and recency.
        severity = round(
            min(group["overrides"] / 10, 1.0) * 0.6
            + override_rate * 0.3
            + _recency_weight(group["last_seen"]) * 0.1,
            4,
        )
        signature = _pattern_signature(domain, reason)
        candidates.append(
            {
                "signature": signature,
                "domain": domain,
                "override_reason": reason,
                "overrides": group["overrides"],
                "domain_total": domain_total,
                "override_rate": override_rate,
                "avg_confidence": avg_confidence,
                "severity": severity,
                "first_seen": group["first_seen"],
                "last_seen": group["last_seen"],
                "sample_task_ids": group["sample_task_ids"][:5],
                "sample_actions": [s for s in group["sample_actions"][:3] if s],
                "recommended_actions": _recommend_actions(domain, group["overrides"], override_rate),
            }
        )

    candidates.sort(key=lambda c: (c["severity"], c["overrides"]), reverse=True)
    return {
        "window_hours": max(1, int(hours or 168)),
        "sampled_outcomes": sampled,
        "domains": domain_totals,
        "learning_candidates": candidates,
        "contract": {
            "mutates_policy": False,
            "uses_llm": False,
            "recommendation": (
                "review candidates before changing autonomy thresholds or recommendation policy"
            ),
        },
    }


def create_override_review_tasks(
    *,
    hours: int = 168,
    min_overrides: int = 2,
    limit: int = 500,
    max_tasks: int = 5,
    db_path: Path | str | None = None,
    task_queue_obj: Any | None = None,
) -> dict:
    """Materialize top override patterns into bounded review tasks.

    Dedupes against open tasks via a stable signature stored in metadata so
    daily runs do not spawn duplicate work items for the same pattern.
    Policy is never mutated: each task is a human-in-the-loop investigation
    request that an agent can pick up.
    """
    report = override_patterns_report(
        hours=hours,
        min_overrides=min_overrides,
        limit=limit,
        db_path=db_path,
    )
    tq = task_queue_obj or _default_task_queue()
    if tq is None:
        return {"created": [], "skipped": [], "error": "task_queue_unavailable", "report": report}

    open_signatures = _open_review_task_signatures(tq)
    created: list[dict] = []
    skipped: list[dict] = []
    for candidate in report.get("learning_candidates", [])[: max(1, int(max_tasks or 5))]:
        signature = candidate["signature"]
        if signature in open_signatures:
            skipped.append({"signature": signature, "reason": "open_task_exists"})
            continue
        task = tq.create_task(
            title=_review_task_title(candidate),
            description=_review_task_description(candidate),
            # 2026-05-13: brain-generated review tasks dispatch through the
            # CLI fallback chain (cli_llm.cli_dispatch → Codex),
            # not OpenClaw agent personas. The "brain_cli" label flags the
            # ownership; the dispatcher filters on created_by, not agent.
            assigned_agent="brain_cli",
            priority=3,
            confidence=float(candidate.get("avg_confidence") or 0.0),
            confidence_reasoning=("outcome_feedback override pattern — review before autonomy promotion"),
            created_by="outcome_feedback",
            metadata={
                "domain": "brain-system",
                "source": "outcome_feedback",
                "override_signature": signature,
                "pattern": {
                    "domain": candidate["domain"],
                    "override_reason": candidate["override_reason"],
                    "overrides": candidate["overrides"],
                    "override_rate": candidate["override_rate"],
                    "severity": candidate["severity"],
                },
                "recommended_actions": candidate["recommended_actions"],
                "mutates_policy": False,
                "uses_llm": True,
                "llm_dispatch": "cli_llm",
            },
        )
        created.append({"signature": signature, "task_id": task.get("id"), "title": task.get("title")})
        open_signatures.add(signature)
    return {"created": created, "skipped": skipped, "report": report}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _empty_report(hours: int, *, reason: str) -> dict:
    return {
        "window_hours": max(1, int(hours or 168)),
        "sampled_outcomes": 0,
        "domains": {},
        "learning_candidates": [],
        "contract": {
            "mutates_policy": False,
            "uses_llm": False,
            "recommendation": "no data — verify autonomy db path",
            "skipped_reason": reason,
        },
    }


def _normalize_reason(raw: object) -> str:
    text = (str(raw or "")).strip()
    if not text:
        return "(unspecified)"
    # Collapse whitespace and cap length so distinct phrasings of the same
    # underlying reason still group together.
    text = " ".join(text.split())
    return text[:160]


def _pattern_signature(domain: str, reason: str) -> str:
    raw = f"{domain.strip().lower()}|{reason.strip().lower()}"
    # sha1 here is a non-cryptographic dedupe key — only used to derive a
    # stable signature that identifies the same (domain, reason) cluster
    # across runs. usedforsecurity=False silences ruff S324 without
    # changing behavior.
    return "ov_" + hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:14]


def _append_sample(bucket: list[str], value: str) -> None:
    if not value or value in bucket or len(bucket) >= 10:
        return
    bucket.append(value)


def _float(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _recency_weight(ts: str) -> float:
    """Map last_seen → 0..1 weight; <24h ago is 1.0, >7d ago is 0."""
    if not ts:
        return 0.0
    try:
        seen = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return 0.0
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=UTC)
    age_h = (datetime.now(UTC) - seen).total_seconds() / 3600
    if age_h <= 24:
        return 1.0
    if age_h >= 168:
        return 0.0
    # Linear decay between 24h and 7d.
    return round(1.0 - (age_h - 24) / (168 - 24), 3)


def _recommend_actions(domain: str, overrides: int, override_rate: float) -> list[str]:
    actions: list[str] = []
    if override_rate >= 0.95:
        actions.append(
            "Domain is fully overridden — freeze recommendations and gather Chris's preferred decision path."
        )
    elif override_rate >= 0.6:
        actions.append(
            "Most decisions in this domain were overridden — re-evaluate the recommendation policy."
        )
    if overrides >= 10:
        actions.append(
            "Cluster the sample task descriptions and surface the dominant override reason "
            "to the autonomy gate."
        )
    actions.append(f"Review the last 3 override samples and propose a counterexample test for {domain}.")
    return actions


def _review_task_title(candidate: dict) -> str:
    domain = candidate["domain"]
    return (
        f"[outcome-feedback] override pattern in '{domain}'"
        f" ({candidate['overrides']}x, {int(candidate['override_rate'] * 100)}%)"
    )


def _review_task_description(candidate: dict) -> str:
    lines = [
        "Override pattern detected by outcome_feedback (read-only).",
        f"Domain: {candidate['domain']}",
        f"Override reason: {candidate['override_reason']}",
        f"Overrides: {candidate['overrides']} of {candidate['domain_total']}"
        f" ({int(candidate['override_rate'] * 100)}%)",
        f"Severity: {candidate['severity']}",
        f"First seen: {candidate['first_seen']}",
        f"Last seen: {candidate['last_seen']}",
        "",
        "Sample task ids:",
        *[f"  - {tid}" for tid in candidate["sample_task_ids"]],
        "",
        "Recommended actions:",
        *[f"  - {a}" for a in candidate["recommended_actions"]],
        "",
        "Contract: do not mutate autonomy thresholds or policy from this task."
        " Surface evidence and propose a change for explicit review.",
    ]
    return "\n".join(lines)


def _open_review_task_signatures(task_queue_obj: Any) -> set[str]:
    signatures: set[str] = set()
    try:
        tasks = task_queue_obj.list_tasks(status="pending") or []
    except Exception:
        tasks = []
    with contextlib.suppress(Exception):
        tasks += task_queue_obj.list_tasks(status="approved") or []
    with contextlib.suppress(Exception):
        tasks += task_queue_obj.list_tasks(status="running") or []
    for task in tasks:
        meta = task.get("metadata") if isinstance(task, dict) else None
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (TypeError, ValueError):
                meta = None
        if isinstance(meta, dict):
            sig = meta.get("override_signature")
            if sig:
                signatures.add(str(sig))
    return signatures


def _default_task_queue() -> Any | None:
    try:
        try:
            from brain_core.task_queue import task_queue
        except ImportError:
            from task_queue import task_queue
        return task_queue
    except Exception as exc:
        log.debug("outcome_feedback default task_queue unavailable: %s", exc)
        return None


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    rep = sub.add_parser("report")
    rep.add_argument("--hours", type=int, default=168)
    rep.add_argument("--min-overrides", type=int, default=2)
    rep.add_argument("--limit", type=int, default=500)
    tasks = sub.add_parser("create_review_tasks")
    tasks.add_argument("--hours", type=int, default=168)
    tasks.add_argument("--min-overrides", type=int, default=2)
    tasks.add_argument("--max-tasks", type=int, default=5)
    tasks.add_argument("--limit", type=int, default=500)
    args = p.parse_args()
    if args.cmd == "report":
        print(
            json.dumps(
                override_patterns_report(
                    hours=args.hours,
                    min_overrides=args.min_overrides,
                    limit=args.limit,
                ),
                indent=2,
                default=str,
            )
        )
    elif args.cmd == "create_review_tasks":
        print(
            json.dumps(
                create_override_review_tasks(
                    hours=args.hours,
                    min_overrides=args.min_overrides,
                    max_tasks=args.max_tasks,
                    limit=args.limit,
                ),
                indent=2,
                default=str,
            )
        )
