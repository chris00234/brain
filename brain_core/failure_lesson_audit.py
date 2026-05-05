"""Outcome audit for Reflexion failure lessons.

The failure-memory loop is only useful if retrieved lessons can be traced to
later task outcomes. This module is intentionally read-only: it inspects the
task outcome ledger and reports whether post-use lesson evidence is available
and healthy enough to act as a readiness signal.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MIN_LINKED_OUTCOMES = 5
MIN_SUCCESS_RATE = 0.60


def _default_db_path() -> Path:
    try:
        from config import BRAIN_LOGS_DIR
    except Exception:
        BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")
    return Path(BRAIN_LOGS_DIR) / "autonomy.db"


def _json_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(v) for v in raw if str(v)]
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(v) for v in parsed if str(v)]


def _active_lesson_count() -> int | None:
    """Return active Lesson count from Neo4j when available.

    ``None`` means the graph check failed, not that there are no lessons.
    """

    try:
        from neo4j_client import run_query

        rows = run_query(
            "MATCH (l:Lesson) WHERE coalesce(l.archived, false) = false " "RETURN count(l) AS count",
            {},
        )
        if not rows:
            return 0
        return int(rows[0].get("count") or 0)
    except Exception:
        return None


def failure_lesson_outcome_snapshot(db_path: Path | str | None = None) -> dict[str, Any]:
    """Summarize task outcomes that were influenced by retrieved failure lessons.

    Status contract:
    - ``ok``: enough linked outcomes and success rate meets threshold.
    - ``insufficient_data``: linkage works, but there are too few real outcomes
      to judge quality yet. This is visible but non-blocking.
    - ``blocked``: enough linked outcomes exist and success rate is below the
      minimum, or the schema cannot record lesson links.
    """

    path = Path(db_path) if db_path is not None else _default_db_path()
    generated_at = datetime.now(UTC).isoformat(timespec="seconds")
    base: dict[str, Any] = {
        "generated_at": generated_at,
        "path": str(path),
        "min_linked_outcomes": MIN_LINKED_OUTCOMES,
        "min_success_rate": MIN_SUCCESS_RATE,
        "readiness_blocking": False,
    }
    if not path.exists():
        return {**base, "status": "insufficient_data", "reason": "autonomy_db_missing"}

    try:
        with sqlite3.connect(str(path)) as conn:
            conn.row_factory = sqlite3.Row
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(outcomes)").fetchall()}
            if "lesson_ids" not in cols:
                return {
                    **base,
                    "status": "blocked",
                    "readiness_blocking": True,
                    "reason": "outcomes.lesson_ids column missing",
                }
            rows = conn.execute(
                "SELECT task_id, chris_override, confidence_was, lesson_ids, created_at "
                "FROM outcomes ORDER BY created_at DESC"
            ).fetchall()
    except Exception as exc:
        return {
            **base,
            "status": "blocked",
            "readiness_blocking": True,
            "reason": str(exc)[:200],
        }

    linked: list[dict[str, Any]] = []
    lesson_counts: dict[str, dict[str, int]] = {}
    for row in rows:
        lesson_ids = _json_list(row["lesson_ids"])
        if not lesson_ids:
            continue
        success = int(row["chris_override"] or 0) == 0
        linked.append(
            {
                "task_id": row["task_id"],
                "lesson_ids": lesson_ids,
                "success": success,
                "confidence_was": row["confidence_was"],
                "created_at": row["created_at"],
            }
        )
        for lesson_id in lesson_ids:
            bucket = lesson_counts.setdefault(lesson_id, {"linked_outcomes": 0, "successes": 0})
            bucket["linked_outcomes"] += 1
            if success:
                bucket["successes"] += 1

    linked_outcomes = len(linked)
    linked_success = sum(1 for row in linked if row["success"])
    linked_failure = linked_outcomes - linked_success
    success_rate = (linked_success / linked_outcomes) if linked_outcomes else None
    lessons_with_outcomes = len(lesson_counts)
    active_lessons = _active_lesson_count()
    if linked_outcomes == 0 and active_lessons == 0:
        status = "no_lessons"
    elif linked_outcomes < MIN_LINKED_OUTCOMES:
        status = "insufficient_data"
    elif success_rate is not None and success_rate >= MIN_SUCCESS_RATE:
        status = "ok"
    else:
        status = "blocked"

    return {
        **base,
        "status": status,
        "readiness_blocking": status not in {"ok", "no_lessons"},
        "linked_outcomes": linked_outcomes,
        "linked_success": linked_success,
        "linked_failure": linked_failure,
        "success_rate": success_rate,
        "lessons_with_outcomes": lessons_with_outcomes,
        "active_lessons": active_lessons,
        "recent": linked[:20],
        "lessons": [
            {"lesson_id": lesson_id, **counts}
            for lesson_id, counts in sorted(
                lesson_counts.items(),
                key=lambda item: (-item[1]["linked_outcomes"], item[0]),
            )[:20]
        ],
    }


if __name__ == "__main__":
    sys.stdout.write(json.dumps(failure_lesson_outcome_snapshot(), indent=2, ensure_ascii=False) + "\n")
