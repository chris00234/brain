"""Task lifecycle hygiene audit and safe repair helpers.

World-class Brain autonomy needs every task to have a clear lifecycle state:
queued, executing, completed, failed, or explicitly blocked with evidence. This
module is intentionally conservative: it reports ambiguous backlog pressure and
only repairs contradictions that are provably safe, such as completed tasks that
still carry a stale error string from an earlier retry.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

try:
    from config import AUTONOMY_DB, BRAIN_LOGS_DIR
except ImportError:  # pragma: no cover - direct script fallback
    BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")
    AUTONOMY_DB = BRAIN_LOGS_DIR / "autonomy.db"

OPEN_STATUSES = {"pending", "approved", "assigned", "running", "paused", "resumed"}
READY_STATUSES = {"approved", "assigned"}
OPEN_TASK_WARNING_TARGET = 100
READY_BACKLOG_WARNING_TARGET = 25
STALE_READY_HOURS = 24
STALE_RUNNING_MINUTES = 60
STALE_STARTED_MINUTES = 15
REPORT_FILE = BRAIN_LOGS_DIR / "task_lifecycle_hygiene.json"


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_dt(raw: Any) -> datetime | None:
    if raw in (None, ""):
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except ValueError:
        return None


def _load_json(raw: Any, default: Any) -> Any:
    if isinstance(raw, dict | list):
        return raw
    if not isinstance(raw, str) or not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


def _norm_text(raw: str) -> str:
    return re.sub(r"\s+", " ", str(raw or "").strip().lower())


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        is not None
    )


def _task_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    if not _has_table(conn, "tasks"):
        return []
    return conn.execute(
        """SELECT id, title, description, status, assigned_agent, created_at,
                  updated_at, started_at, completed_at, error, execution_log, metadata
           FROM tasks"""
    ).fetchall()


def _dispatch_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    if not _has_table(conn, "task_dispatch_attempts"):
        return []
    return conn.execute(
        """SELECT id, task_id, status, error_class, error, started_at, completed_at, metadata
           FROM task_dispatch_attempts"""
    ).fetchall()


def _ready_blocked_by_future_retry(metadata: dict[str, Any], now: datetime) -> bool:
    next_attempt_at = _parse_dt(metadata.get("next_attempt_at"))
    return bool(next_attempt_at and next_attempt_at > now)


def _classifier_hold_redrivable(row: sqlite3.Row, metadata: dict[str, Any]) -> bool:
    """Return True only when the current deterministic policy no longer holds it.

    Historical classifier holds are ambiguous by themselves: some are stale
    false positives, but some still require Chris/private authority. The hygiene
    audit should block readiness only on the former. The redrive CLI performs
    the same current-policy check before writing.
    """

    try:
        from escalation_policy import classify_escalation

        route = classify_escalation(
            title=str(row["title"] or ""),
            content=str(row["description"] or ""),
            metadata=metadata,
        )
    except Exception:
        return False
    return not route.notify_human


def audit_task_lifecycle(
    db_path: Path | str = AUTONOMY_DB,
    *,
    now: datetime | None = None,
    duplicate_examples: int = 20,
) -> dict[str, Any]:
    """Return a deterministic task lifecycle hygiene report."""

    now = now or _now()
    db_path = Path(db_path)
    if not db_path.exists():
        return {
            "generated_at": now.isoformat(timespec="seconds"),
            "status": "missing_db",
            "readiness_blocking": True,
            "db": str(db_path),
        }

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        tasks = _task_rows(conn)
        attempts = _dispatch_rows(conn)

    status_counts = Counter(str(row["status"] or "unknown") for row in tasks)
    open_tasks = [row for row in tasks if str(row["status"] or "") in OPEN_STATUSES]
    completed_with_error = [row for row in tasks if row["status"] == "completed" and str(row["error"] or "")]
    pending_policy_held = []
    classifier_held_redrivable = []
    classifier_held_still_human = []
    ready_backlog = []
    stale_ready = []
    stale_running = []
    exact_groups: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)

    for row in open_tasks:
        metadata = _load_json(row["metadata"], {})
        if not isinstance(metadata, dict):
            metadata = {}
        if metadata.get("task_evaluation_decision") == "policy_held":
            pending_policy_held.append(row)
            if metadata.get("task_evaluation_source") == "classifier_human_required":
                if _classifier_hold_redrivable(row, metadata):
                    classifier_held_redrivable.append(row)
                else:
                    classifier_held_still_human.append(row)
        if row["status"] in READY_STATUSES and not _ready_blocked_by_future_retry(metadata, now):
            ready_backlog.append(row)
            updated = _parse_dt(row["updated_at"]) or _parse_dt(row["created_at"])
            if updated and updated <= now - timedelta(hours=STALE_READY_HOURS):
                stale_ready.append(row)
        if row["status"] == "running":
            started = _parse_dt(row["started_at"]) or _parse_dt(row["updated_at"])
            if started and started <= now - timedelta(minutes=STALE_RUNNING_MINUTES):
                stale_running.append(row)
        exact_groups[(_norm_text(row["title"]), _norm_text(row["description"]))].append(row)

    duplicate_groups = [rows for key, rows in exact_groups.items() if key[0] and len(rows) > 1]
    duplicate_shadow_count = sum(len(rows) - 1 for rows in duplicate_groups)
    duplicate_examples_rows = []
    for rows in sorted(duplicate_groups, key=lambda group: (-len(group), group[0]["created_at"] or ""))[
        :duplicate_examples
    ]:
        duplicate_examples_rows.append(
            {
                "canonical_task_id": rows[0]["id"],
                "duplicate_task_ids": [row["id"] for row in rows[1:]],
                "count": len(rows),
                "title": rows[0]["title"],
                "statuses": [row["status"] for row in rows],
            }
        )

    cutoff_started = now - timedelta(minutes=STALE_STARTED_MINUTES)
    stale_started_attempts = []
    deferred_attempts = []
    for row in attempts:
        if row["status"] == "started":
            started = _parse_dt(row["started_at"])
            if started and started <= cutoff_started:
                stale_started_attempts.append(row)
        if row["status"] == "deferred":
            deferred_attempts.append(row)

    critical_issue_count = (
        len(completed_with_error)
        + len(stale_running)
        + len(stale_started_attempts)
        + len(classifier_held_redrivable)
    )
    warning_issue_count = 0
    if len(open_tasks) > OPEN_TASK_WARNING_TARGET:
        warning_issue_count += len(open_tasks) - OPEN_TASK_WARNING_TARGET
    if len(ready_backlog) > READY_BACKLOG_WARNING_TARGET:
        warning_issue_count += len(ready_backlog) - READY_BACKLOG_WARNING_TARGET
    warning_issue_count += duplicate_shadow_count

    status = "blocked" if critical_issue_count else "warning" if warning_issue_count else "ok"
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "status": status,
        "readiness_blocking": critical_issue_count > 0,
        "db": str(db_path),
        "critical_issue_count": critical_issue_count,
        "warning_issue_count": warning_issue_count,
        "open_task_count": len(open_tasks),
        "open_task_warning_target": OPEN_TASK_WARNING_TARGET,
        "ready_backlog_count": len(ready_backlog),
        "ready_backlog_warning_target": READY_BACKLOG_WARNING_TARGET,
        "stale_ready_backlog_count": len(stale_ready),
        "completed_with_error_count": len(completed_with_error),
        "pending_policy_held_count": len(pending_policy_held),
        "classifier_held_redrivable_count": len(classifier_held_redrivable),
        "classifier_held_still_human_count": len(classifier_held_still_human),
        "stale_running_task_count": len(stale_running),
        "stale_started_dispatch_count": len(stale_started_attempts),
        "deferred_dispatch_attempt_count": len(deferred_attempts),
        "duplicate_exact_group_count": len(duplicate_groups),
        "duplicate_exact_shadow_count": duplicate_shadow_count,
        "status_counts": dict(status_counts),
        "samples": {
            "completed_with_error_task_ids": [row["id"] for row in completed_with_error[:20]],
            "classifier_held_redrivable_task_ids": [row["id"] for row in classifier_held_redrivable[:20]],
            "classifier_held_still_human_task_ids": [row["id"] for row in classifier_held_still_human[:20]],
            "stale_running_task_ids": [row["id"] for row in stale_running[:20]],
            "stale_started_dispatch_ids": [row["id"] for row in stale_started_attempts[:20]],
            "oldest_ready_task_ids": [row["id"] for row in stale_ready[:20]],
            "duplicate_exact_groups": duplicate_examples_rows,
        },
        "recommended_safe_repairs": [
            "cli/task_lifecycle_hygiene.py --apply-safe clears completed-task stale errors",
            "cli/redrive_held_tasks.py redrives classifier-held tasks after policy fixes",
        ],
    }


def task_lifecycle_hygiene_issue_count(db_path: Path | str = AUTONOMY_DB) -> int:
    report = audit_task_lifecycle(db_path)
    return int(report.get("critical_issue_count") or 0)


def readiness_snapshot(db_path: Path | str = AUTONOMY_DB) -> dict[str, Any]:
    return audit_task_lifecycle(db_path)


def apply_safe_repairs(db_path: Path | str = AUTONOMY_DB) -> dict[str, Any]:
    """Apply only evidence-preserving, non-destructive lifecycle repairs."""

    db_path = Path(db_path)
    summary = {
        "status": "ok",
        "db": str(db_path),
        "completed_errors_cleared": 0,
    }
    if not db_path.exists():
        return {**summary, "status": "missing_db"}
    now = _now().isoformat(timespec="seconds")
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        if not _has_table(conn, "tasks"):
            return {**summary, "status": "missing_tasks_table"}
        rows = conn.execute(
            "SELECT id, execution_log FROM tasks WHERE status='completed' AND COALESCE(error, '') <> ''"
        ).fetchall()
        for row in rows:
            execution_log = _load_json(row["execution_log"], [])
            if not isinstance(execution_log, list):
                execution_log = []
            execution_log.append(
                {
                    "event": "completed_error_cleared",
                    "by": "task_lifecycle_hygiene",
                    "reason": "completed task carried stale error after successful completion",
                    "at": now,
                }
            )
            conn.execute(
                "UPDATE tasks SET error='', execution_log=?, updated_at=? WHERE id=?",
                (json.dumps(execution_log), now, row["id"]),
            )
        conn.commit()
    summary["completed_errors_cleared"] = len(rows)
    return summary


def write_report(report: dict[str, Any], path: Path | str = REPORT_FILE) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
