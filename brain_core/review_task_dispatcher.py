"""brain_core/review_task_dispatcher.py — pick up the oldest few brain-
generated review tasks and dispatch them through cli_llm.

`outcome_feedback` and `goal_subtask_scaffold` create review/work tasks
labelled with `assigned_agent='brain_cli'`. Nothing was picking them up
until this module shipped, so the override-pattern → action loop was
still open.

The dispatcher uses `cli_llm.dispatch` (codex → claude fallback through
the ChatGPT/Claude subscription CLIs) instead of routing through an
OpenClaw agent. Same brain-format that recall_judge, goal_decompose,
and friends use — stateless prompt, subscription-bounded cost, no
agent-persona side effects. Bounded by `MAX_DISPATCHES_PER_RUN` to
avoid burning the quota when many patterns surface at once.

Read-only with respect to autonomy policy. The dispatched message is
the task description verbatim — the CLI response is recorded back as
the task result and Chris reviews it later.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

log = logging.getLogger("brain.review_task_dispatcher")


MAX_DISPATCHES_PER_RUN = 2  # caps daily CLI spend on review work
DISPATCH_TIMEOUT_SEC = 180
SOURCES = ("outcome_feedback", "goal_subtask_scaffold", "decision_feedback")
# Brain-generated review/work tasks carry this label so the dispatcher
# can filter them from operator- or agent-assigned tasks. The CLI path
# (cli_llm.dispatch → codex/claude) ignores the agent label internally;
# we keep it for visibility and to gate eligibility here.
BRAIN_CLI_AGENT_LABEL = "brain_cli"


def dispatch_pending_review_tasks(
    *,
    max_dispatches: int = MAX_DISPATCHES_PER_RUN,
    task_queue_obj: Any | None = None,
    dispatch_fn: Any | None = None,
) -> dict:
    """Claim up to `max_dispatches` pending review tasks and dispatch via cli_llm."""
    tq = task_queue_obj or _default_task_queue()
    if tq is None:
        return {"dispatched": [], "skipped": [], "error": "task_queue_unavailable"}

    pending = _candidate_tasks(tq)
    if not pending:
        return {"dispatched": [], "skipped": [], "reason": "no_eligible_tasks"}

    dispatcher = dispatch_fn or _default_dispatcher()
    if dispatcher is None:
        return {"dispatched": [], "skipped": [], "error": "cli_llm_unavailable"}

    cap = max(1, int(max_dispatches or MAX_DISPATCHES_PER_RUN))
    dispatched: list[dict] = []
    skipped: list[dict] = []
    for task in pending[:cap]:
        message = _build_message(task)
        try:
            tq._transition(task["id"], {"pending", "approved"}, "running", by="review_task_dispatcher")
        except Exception as exc:
            skipped.append({"task_id": task["id"], "reason": f"claim_failed:{str(exc)[:80]}"})
            continue
        try:
            result = dispatcher(prompt=message, timeout=DISPATCH_TIMEOUT_SEC)
        except Exception as exc:
            log.warning("cli_llm dispatch raised for %s: %s", task["id"], exc)
            _safe_fail(tq, task["id"], f"dispatch_exception:{str(exc)[:200]}")
            skipped.append({"task_id": task["id"], "reason": "dispatch_exception"})
            continue
        ok = bool(getattr(result, "ok", False))
        text = getattr(result, "text", "")
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
                        "backend": getattr(result, "backend", "") or getattr(result, "provider", ""),
                        "model": getattr(result, "model", ""),
                        "preview": (text or "")[:200],
                    }
                )
            except Exception as exc:
                log.warning("complete_task failed for %s: %s", task["id"], exc)
                skipped.append({"task_id": task["id"], "reason": f"complete_failed:{str(exc)[:80]}"})
        else:
            err = getattr(result, "error", "") or "degraded"
            _safe_fail(tq, task["id"], f"cli_dispatch_failed:{err[:200]}")
            skipped.append({"task_id": task["id"], "reason": "cli_dispatch_failed"})

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
    """Oldest-first pending/approved tasks created by the brain review pipelines.

    Eligibility is gated by `created_by` (must be a brain-generated source)
    so operator- or agent-assigned tasks are never auto-dispatched.

    Pulls both `pending` and `approved` because task_queue.process_pending
    auto-approves brain_cli tasks before this dispatcher's cron fires. The
    downstream `_transition` already accepts {pending, approved}, so the
    candidate query just has to match.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for status in ("pending", "approved"):
        try:
            rows = task_queue_obj.list_tasks(status=status) or []
        except Exception as exc:
            log.debug("list_tasks(status=%s) failed: %s", status, exc)
            continue
        for task in rows:
            if not isinstance(task, dict):
                continue
            tid = task.get("id") or ""
            if tid in seen:
                continue
            if (task.get("created_by") or "") not in SOURCES:
                continue
            if (task.get("assigned_agent") or "") != BRAIN_CLI_AGENT_LABEL:
                continue
            seen.add(tid)
            out.append(task)
    out.sort(key=lambda t: str(t.get("created_at") or ""))
    return out


def _build_message(task: dict) -> str:
    meta = task.get("metadata") or {}
    metric = meta.get("brain_quality_metric") or meta.get("override_signature") or ""
    src = meta.get("source") or task.get("created_by") or "brain"
    lines = [
        "You are processing a brain-generated review task. The brain detected a pattern",
        "and is asking for a concrete investigation: surface evidence and propose a",
        "counterexample test or remediation step. Do NOT mutate autonomy thresholds or",
        "policy directly — write the proposal as text and stop.",
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
            from brain_core.cli_llm import cli_dispatch
        except ImportError:
            from cli_llm import cli_dispatch
        return cli_dispatch
    except Exception as exc:
        log.debug("cli_llm.cli_dispatch unavailable: %s", exc)
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
