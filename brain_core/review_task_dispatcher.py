"""brain_core/review_task_dispatcher.py — pick up the oldest few brain-
generated review tasks and dispatch them to Sage.

`outcome_feedback` and `goal_subtask_scaffold` create review/work tasks
assigned to Sage, but nothing was actually picking them up. The tasks
sat 'pending' forever — the override-pattern → action loop was still
open.

This module closes it: each daily run claims the oldest N pending
tasks created by those two pipelines, dispatches the description to
Sage via `openclaw_dispatch`, and records the result back onto the
task (completed on success, failed with error on degraded). Bounded
by `MAX_DISPATCHES_PER_RUN` to avoid burning subscription quota when
many patterns surface.

Read-only with respect to autonomy policy. The dispatched message is
the task description verbatim — Sage decides what to do and writes
back through the normal OpenClaw channel.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

log = logging.getLogger("brain.review_task_dispatcher")


MAX_DISPATCHES_PER_RUN = 2  # caps daily sage spend on review work
DISPATCH_TIMEOUT_SEC = 180
SOURCES = ("outcome_feedback", "goal_subtask_scaffold")
DISPATCH_AGENTS = ("sage",)


def dispatch_pending_review_tasks(
    *,
    max_dispatches: int = MAX_DISPATCHES_PER_RUN,
    task_queue_obj: Any | None = None,
    dispatch_fn: Any | None = None,
) -> dict:
    """Claim up to `max_dispatches` pending review tasks and send them to Sage."""
    tq = task_queue_obj or _default_task_queue()
    if tq is None:
        return {"dispatched": [], "skipped": [], "error": "task_queue_unavailable"}

    pending = _candidate_tasks(tq)
    if not pending:
        return {"dispatched": [], "skipped": [], "reason": "no_eligible_tasks"}

    dispatcher = dispatch_fn or _default_dispatcher()
    if dispatcher is None:
        return {"dispatched": [], "skipped": [], "error": "openclaw_dispatch_unavailable"}

    cap = max(1, int(max_dispatches or MAX_DISPATCHES_PER_RUN))
    dispatched: list[dict] = []
    skipped: list[dict] = []
    for task in pending[:cap]:
        agent = (task.get("assigned_agent") or "sage").lower()
        if agent not in DISPATCH_AGENTS:
            skipped.append({"task_id": task["id"], "reason": f"agent_not_allowed:{agent}"})
            continue
        message = _build_message(task)
        try:
            tq._transition(task["id"], {"pending", "approved"}, "running", by="review_task_dispatcher")
        except Exception as exc:
            skipped.append({"task_id": task["id"], "reason": f"claim_failed:{str(exc)[:80]}"})
            continue
        try:
            result = dispatcher(
                agent=agent,
                message=message,
                thinking="low",
                timeout=DISPATCH_TIMEOUT_SEC,
                session_id="brain_dispatch_sage_review",
            )
        except Exception as exc:
            log.warning("dispatch raised for %s: %s", task["id"], exc)
            _safe_fail(tq, task["id"], f"dispatch_exception:{str(exc)[:200]}")
            skipped.append({"task_id": task["id"], "reason": "dispatch_exception"})
            continue
        ok = bool(getattr(result, "ok", False))
        text = getattr(result, "text", "") or getattr(result, "degraded", "")
        if ok:
            try:
                tq.complete_task(
                    task["id"],
                    result=(text or "")[:3000],
                    by="review_task_dispatcher",
                )
                dispatched.append(
                    {
                        "task_id": task["id"],
                        "title": task.get("title"),
                        "duration_ms": getattr(result, "duration_ms", None),
                        "attempts": getattr(result, "attempts", None),
                        "preview": (text or "")[:200],
                    }
                )
            except Exception as exc:
                log.warning("complete_task failed for %s: %s", task["id"], exc)
                skipped.append({"task_id": task["id"], "reason": f"complete_failed:{str(exc)[:80]}"})
        else:
            err = getattr(result, "error", "") or "degraded"
            _safe_fail(tq, task["id"], f"sage_dispatch_failed:{err[:200]}")
            skipped.append({"task_id": task["id"], "reason": "sage_dispatch_failed"})

    return {
        "dispatched": dispatched,
        "skipped": skipped,
        "candidate_total": len(pending),
        "cap": cap,
        "ts": _now_iso(),
    }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _candidate_tasks(task_queue_obj: Any) -> list[dict]:
    """Oldest-first pending tasks created by the review pipelines."""
    out: list[dict] = []
    try:
        rows = task_queue_obj.list_tasks(status="pending") or []
    except Exception:
        return []
    for task in rows:
        if not isinstance(task, dict):
            continue
        if (task.get("created_by") or "") not in SOURCES:
            continue
        if (task.get("assigned_agent") or "").lower() not in DISPATCH_AGENTS:
            continue
        out.append(task)
    out.sort(key=lambda t: str(t.get("created_at") or ""))
    return out


def _build_message(task: dict) -> str:
    meta = task.get("metadata") or {}
    metric = meta.get("brain_quality_metric") or meta.get("override_signature") or ""
    src = meta.get("source") or task.get("created_by") or "brain"
    lines = [
        "You are picking up a brain-generated review task. The brain detected a pattern",
        "and is asking you to investigate without mutating policy. Surface evidence and",
        "propose a counterexample test or concrete remediation step.",
        "",
        f"Source: {src}",
    ]
    if metric:
        lines.append(f"Pattern key: {metric}")
    lines.append("")
    lines.append("Task:")
    lines.append(task.get("description") or task.get("title") or "(no description)")
    return "\n".join(lines)


def _safe_fail(task_queue_obj: Any, task_id: str, error: str) -> None:
    try:
        task_queue_obj.fail_task(task_id, error=error[:500], by="review_task_dispatcher")
    except Exception as exc:
        log.debug("fail_task fallback raised for %s: %s", task_id, exc)


def _default_task_queue() -> Any | None:
    try:
        try:
            from brain_core.task_queue import task_queue
        except ImportError:
            from task_queue import task_queue
        return task_queue
    except Exception as exc:
        log.debug("default task_queue unavailable: %s", exc)
        return None


def _default_dispatcher() -> Any | None:
    try:
        try:
            from brain_core.openclaw_dispatch import dispatch
        except ImportError:
            from openclaw_dispatch import dispatch
        return dispatch
    except Exception as exc:
        log.debug("openclaw_dispatch unavailable: %s", exc)
        return None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


if __name__ == "__main__":
    import argparse
    import json

    p = argparse.ArgumentParser()
    p.add_argument("--max", dest="max_dispatches", type=int, default=MAX_DISPATCHES_PER_RUN)
    args = p.parse_args()
    print(
        json.dumps(
            dispatch_pending_review_tasks(max_dispatches=args.max_dispatches),
            indent=2,
            default=str,
        )
    )
