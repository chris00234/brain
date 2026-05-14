"""Work status — proactive surface of running / failed / queued scheduler jobs.

Reads brain_scheduler in-memory state for live job posture and
`scheduler_history.db` for recent failures. Pure read, no LLM.

Boot-context contract:
- Surface ONLY when there is something the operator should know:
  a job is running, a job failed recently, or a job is deferred.
- Stay terse: boot context already loads ~15 blocks; this adds at most
  one short block.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

_SCHED_HISTORY_DB = Path("/Users/chrischo/server/brain/logs/scheduler_history.db")
_DEFAULT_FAILURE_WINDOW_HOURS = 24


def _recent_failures(window_hours: int) -> list[dict]:
    if not _SCHED_HISTORY_DB.exists():
        return []
    try:
        conn = sqlite3.connect(str(_SCHED_HISTORY_DB), timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT job_name, MAX(started_at) AS last_failed_at, COUNT(*) AS failures
                FROM job_history
                WHERE error IS NOT NULL
                  AND error != ''
                  AND started_at > datetime('now', ?)
                GROUP BY job_name
                ORDER BY failures DESC, last_failed_at DESC
                """,
                (f"-{int(window_hours)} hours",),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _scheduler_snapshot() -> list[dict]:
    try:
        from scheduler import brain_scheduler

        return list(brain_scheduler.list_jobs())
    except Exception:
        return []


def compute_status(window_hours: int = _DEFAULT_FAILURE_WINDOW_HOURS) -> dict[str, Any]:
    """Return running, failed, and deferred job summaries."""
    snapshot = _scheduler_snapshot()
    failures = _recent_failures(window_hours)

    running: list[dict] = []
    deferred: list[dict] = []
    next_runs: list[dict] = []

    for job in snapshot:
        if job.get("running_pid"):
            running.append(
                {
                    "name": job.get("name"),
                    "pid": job.get("running_pid"),
                    "last_run": job.get("last_run"),
                }
            )
        if job.get("resource_defer"):
            deferred.append(
                {
                    "name": job.get("name"),
                    "reason": job.get("resource_defer"),
                }
            )
        next_run = job.get("next_run")
        if next_run:
            next_runs.append({"name": job.get("name"), "next_run": next_run})

    next_runs.sort(key=lambda r: r["next_run"])
    return {
        "window_hours": window_hours,
        "running": running,
        "recent_failures": failures,
        "deferred": deferred,
        "next_runs": next_runs[:5],
        "totals": {
            "running": len(running),
            "failed": len(failures),
            "deferred": len(deferred),
        },
    }


def boot_context_block(window_hours: int = _DEFAULT_FAILURE_WINDOW_HOURS) -> str | None:
    """Format work status as a boot-context block. Returns None when there
    is nothing the operator needs to know."""
    status = compute_status(window_hours=window_hours)
    if not any(status["totals"].values()):
        return None

    lines: list[str] = []
    for job in status["running"][:3]:
        lines.append(f"- RUNNING: {job['name']} (pid {job['pid']})")
    for fail in status["recent_failures"][:3]:
        ts = (fail.get("last_failed_at") or "")[:16].replace("T", " ")
        n = fail.get("failures", 0)
        plural = "x" if n != 1 else ""
        lines.append(f"- FAILED ({window_hours}h): {fail['job_name']} {n}{plural} (last {ts})")
    for job in status["deferred"][:3]:
        lines.append(f"- DEFERRED: {job['name']} — {job['reason']}")
    return "\n".join(lines) if lines else None


__all__ = ["boot_context_block", "compute_status"]
