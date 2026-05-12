#!/usr/bin/env python3
"""Re-drive classifier-held tasks after an escalation-policy fix.

This is a narrow recovery tool: it only revisits pending tasks that were held by
Brain's deterministic classifier, not tasks where a subscription LLM judged that
Chris was needed.  If the current classifier now says the task is LLM-handleable,
it clears the escalation cooldown and marks the task for the normal task_queue
loop to evaluate/dispatch on its next tick.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BRAIN_CORE = ROOT / "brain_core"
if str(BRAIN_CORE) not in sys.path:
    sys.path.insert(0, str(BRAIN_CORE))

try:
    from config import AUTONOMY_DB
except ImportError:  # pragma: no cover - direct script fallback
    AUTONOMY_DB = ROOT / "logs" / "autonomy.db"

from escalation_policy import classify_escalation  # noqa: E402


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _load_json(raw: Any, default: Any) -> Any:
    if isinstance(raw, dict | list):
        return raw
    if not isinstance(raw, str) or not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


def _candidate_rows(conn: sqlite3.Connection, limit: int | None) -> list[sqlite3.Row]:
    query = """
        SELECT id, title, description, status, assigned_agent, execution_log, metadata
        FROM tasks
        WHERE status = 'pending'
          AND json_extract(metadata, '$.task_evaluation_decision') = 'policy_held'
          AND json_extract(metadata, '$.task_evaluation_source') = 'classifier_human_required'
        ORDER BY updated_at ASC, created_at ASC
    """
    params: tuple[Any, ...] = ()
    if limit is not None:
        query += " LIMIT ?"
        params = (int(limit),)
    return conn.execute(query, params).fetchall()


def redrive_held_tasks(
    db_path: Path | str = AUTONOMY_DB, *, limit: int | None = None, dry_run: bool = False
) -> dict[str, Any]:
    db_path = Path(db_path)
    summary: dict[str, Any] = {
        "status": "ok",
        "db": str(db_path),
        "dry_run": dry_run,
        "scanned": 0,
        "redriven": 0,
        "still_held": 0,
        "skipped": 0,
        "errors": 0,
        "redriven_task_ids": [],
        "still_held_task_ids": [],
    }
    if not db_path.exists():
        return {**summary, "status": "missing_db"}

    now = _now()
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = _candidate_rows(conn, limit)
        summary["scanned"] = len(rows)
        for row in rows:
            task_id = str(row["id"])
            metadata = _load_json(row["metadata"], {})
            if not isinstance(metadata, dict):
                metadata = {}
            route = classify_escalation(
                title=str(row["title"] or ""),
                content=str(row["description"] or ""),
                metadata=metadata,
            )
            if route.notify_human:
                summary["still_held"] += 1
                summary["still_held_task_ids"].append(task_id)
                continue

            summary["redriven"] += 1
            summary["redriven_task_ids"].append(task_id)
            if dry_run:
                continue

            previous = {
                "decision": metadata.get("task_evaluation_decision"),
                "source": metadata.get("task_evaluation_source"),
                "action": metadata.get("task_evaluation_action"),
                "reason": metadata.get("task_evaluation_reason"),
                "routed_at": metadata.get("task_evaluation_routed_at"),
                "last_escalated_at": metadata.get("last_escalated_at"),
            }
            metadata.pop("last_escalated_at", None)
            metadata.update(
                {
                    "task_evaluation_decision": "pending_redrive",
                    "task_evaluation_source": "classifier_redrive",
                    "task_evaluation_action": "queued_for_re_evaluation",
                    "task_evaluation_brain_action": "cleared_classifier_hold_cooldown",
                    "task_evaluation_reason": f"reclassified_by_current_policy:{route.reason}",
                    "task_evaluation_routed_at": now,
                    "task_evaluation_next_evidence": f"/brain/tasks/{task_id}/execution",
                    "task_evaluation_redrive_at": now,
                    "task_evaluation_redrive_previous": previous,
                }
            )
            execution_log = _load_json(row["execution_log"], [])
            if not isinstance(execution_log, list):
                execution_log = []
            execution_log.append(
                {
                    "event": "task_evaluation_redrive",
                    "by": "classifier_redrive",
                    "decision": "reclassified_handleable",
                    "action": "queued_for_re_evaluation",
                    "brain_action": "cleared_classifier_hold_cooldown",
                    "reason": route.reason,
                    "evidence": f"/brain/tasks/{task_id}/execution",
                    "at": now,
                }
            )
            conn.execute(
                "UPDATE tasks SET metadata = ?, execution_log = ?, updated_at = ? WHERE id = ?",
                (json.dumps(metadata), json.dumps(execution_log), now, task_id),
            )
        if not dry_run:
            conn.commit()
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=AUTONOMY_DB, help="Path to autonomy.db")
    parser.add_argument("--limit", type=int, default=None, help="Maximum candidate rows to scan")
    parser.add_argument(
        "--dry-run", action="store_true", help="Report what would be redriven without writing"
    )
    args = parser.parse_args(argv)
    try:
        summary = redrive_held_tasks(args.db, limit=args.limit, dry_run=args.dry_run)
    except sqlite3.Error as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary.get("status") in {"ok", "missing_db"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
