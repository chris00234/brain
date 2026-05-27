#!/usr/bin/env python3
"""Mint LESSON nodes from repeating chris_override patterns.

Closes the loop where outcome_feedback.override_patterns_report surfaces a
high-recurrence signature (e.g. ``ov_d86d5e11cb50e7`` infra, 24x at 96%
override rate) without any artifact the pretool_brain_nudge hook can
retrieve at the next decision. With LESSON nodes minted, the hook's
get_similar_lessons query will surface the warning before the same wrong
recommendation is published again.

Deterministic by design — uses authoritative correction text from
outcomes, no LLM, idempotent via Neo4j MERGE on lesson_id.

Designed to run as a scheduled job. Reuses the same Neo4j Lesson graph
as failure_memory.record_infra_failure_lesson so the existing
pretool_brain_nudge retrieval picks it up unchanged.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from failure_memory import record_override_pattern_lesson
from outcome_feedback import override_patterns_report

log = logging.getLogger("brain.override_pattern_lesson_mint")

DEFAULT_MIN_OVERRIDES = 3
DEFAULT_MIN_SEVERITY = 0.5
DEFAULT_LOOKBACK_HOURS = 168


def mint_from_patterns(
    *,
    hours: int = DEFAULT_LOOKBACK_HOURS,
    min_overrides: int = DEFAULT_MIN_OVERRIDES,
    min_severity: float = DEFAULT_MIN_SEVERITY,
    dry_run: bool = False,
) -> dict:
    """Walk the override patterns report and mint a LESSON per high-severity row."""
    report = override_patterns_report(hours=hours, min_overrides=min_overrides)
    candidates = report.get("learning_candidates") or []
    minted: list[dict] = []
    skipped: list[dict] = []
    for cand in candidates:
        severity = float(cand.get("severity") or 0.0)
        overrides = int(cand.get("overrides") or 0)
        signature = str(cand.get("signature") or "")
        domain = str(cand.get("domain") or "general")
        sample_actions = list(cand.get("sample_actions") or [])
        if severity < min_severity or overrides < min_overrides or not signature:
            skipped.append(
                {
                    "signature": signature,
                    "reason": "below_threshold",
                    "severity": severity,
                    "overrides": overrides,
                }
            )
            continue
        sample_recommendation = _lookup_sample_brain_recommendation(cand)
        if dry_run:
            minted.append(
                {
                    "signature": signature,
                    "domain": domain,
                    "overrides": overrides,
                    "lesson_id": None,
                    "dry_run": True,
                }
            )
            continue
        lesson_id = record_override_pattern_lesson(
            signature=signature,
            domain=domain,
            overrides=overrides,
            sample_brain_recommendation=sample_recommendation,
            sample_corrections=sample_actions,
        )
        minted.append(
            {
                "signature": signature,
                "domain": domain,
                "overrides": overrides,
                "lesson_id": lesson_id,
                "minted": lesson_id is not None,
            }
        )
    return {
        "window_hours": hours,
        "candidates_seen": len(candidates),
        "minted": minted,
        "skipped": skipped,
        "min_overrides": min_overrides,
        "min_severity": min_severity,
    }


def _lookup_sample_brain_recommendation(candidate: dict) -> str:
    """Pull a sample brain_recommendation for the signature from outcomes."""
    sample_ids = candidate.get("sample_task_ids") or []
    if not sample_ids:
        return ""
    import sqlite3

    try:
        from brain_core.config import AUTONOMY_DB  # type: ignore[import-not-found]
    except ImportError:
        try:
            from config import AUTONOMY_DB
        except ImportError:
            AUTONOMY_DB = Path("/Users/chrischo/server/brain/logs/autonomy.db")
    if not Path(AUTONOMY_DB).exists():
        return ""
    try:
        conn = sqlite3.connect(str(AUTONOMY_DB), timeout=3)
        try:
            row = conn.execute(
                "SELECT brain_recommendation FROM outcomes WHERE task_id = ? LIMIT 1",
                (sample_ids[0],),
            ).fetchone()
            return (row[0] if row else "") or ""
        finally:
            conn.close()
    except sqlite3.Error:
        return ""


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--hours", type=int, default=DEFAULT_LOOKBACK_HOURS)
    p.add_argument("--min-overrides", type=int, default=DEFAULT_MIN_OVERRIDES)
    p.add_argument("--min-severity", type=float, default=DEFAULT_MIN_SEVERITY)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    result = mint_from_patterns(
        hours=args.hours,
        min_overrides=args.min_overrides,
        min_severity=args.min_severity,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
