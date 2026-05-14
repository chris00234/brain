"""brain_core/review_task_dispatcher.py — pick up the oldest few brain-
generated review tasks and dispatch them through cli_llm.

`outcome_feedback` and `goal_subtask_scaffold` create review/work tasks
labelled with `assigned_agent='brain_cli'`. Nothing was picking them up
until this module shipped, so the override-pattern → action loop was
still open.

The dispatcher uses `cli_llm.dispatch` (Codex gpt-5.5 primary through
the ChatGPT subscription CLI) instead of routing through an
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
# (cli_llm.dispatch → Codex) ignores the agent label internally;
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
        attempt = _record_attempt_start(tq, task, message)
        try:
            result = dispatcher(
                prompt=message,
                timeout=DISPATCH_TIMEOUT_SEC,
                allow_openclaw_fallback=False,
            )
        except Exception as exc:
            log.warning("cli_llm dispatch raised for %s: %s", task["id"], exc)
            err = f"dispatch_exception:{str(exc)[:200]}"
            _finish_attempt(
                tq,
                attempt,
                status="deferred" if _is_transient_dispatch_error(tq, err) else "failed",
                error_class="dispatch_exception",
                error=err,
            )
            if _is_transient_dispatch_error(tq, err):
                _safe_defer(tq, task["id"], err)
                skipped.append({"task_id": task["id"], "reason": "dispatch_deferred"})
            else:
                _safe_fail(tq, task["id"], err)
                skipped.append({"task_id": task["id"], "reason": "dispatch_exception"})
            continue
        ok = bool(getattr(result, "ok", False))
        text = getattr(result, "text", "")
        if ok:
            try:
                _finish_attempt(
                    tq,
                    attempt,
                    status="completed",
                    backend=getattr(result, "backend", "") or getattr(result, "provider", ""),
                    model=getattr(result, "model", ""),
                    result_preview=(text or "")[:500],
                    response_chars=len(text or ""),
                    duration_ms=getattr(result, "duration_ms", 0),
                    metadata={
                        "attempts": getattr(result, "attempts", 0),
                        "provider": getattr(result, "provider", ""),
                        "openclaw_fallback_allowed": False,
                    },
                )
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
                _finish_attempt(
                    tq,
                    attempt,
                    status="failed",
                    error_class="complete_failed",
                    error=str(exc)[:500],
                )
                _safe_fail(tq, task["id"], f"complete_failed:{str(exc)[:200]}")
                skipped.append({"task_id": task["id"], "reason": f"complete_failed:{str(exc)[:80]}"})
        else:
            err = getattr(result, "error", "") or "degraded"
            transient = _is_transient_dispatch_error(tq, err)
            _finish_attempt(
                tq,
                attempt,
                status="deferred" if transient else "failed",
                backend=getattr(result, "backend", "") or getattr(result, "provider", ""),
                model=getattr(result, "model", ""),
                error_class="transient_dispatch" if transient else "terminal_dispatch",
                error=err[:500],
                duration_ms=getattr(result, "duration_ms", 0),
                metadata={
                    "attempts": getattr(result, "attempts", 0),
                    "rate_limited": getattr(result, "rate_limited", False),
                    "openclaw_fallback_allowed": False,
                },
            )
            if transient:
                _safe_defer(tq, task["id"], f"cli_dispatch_failed:{err[:200]}")
                skipped.append({"task_id": task["id"], "reason": "cli_dispatch_deferred"})
            else:
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
            rows = (
                task_queue_obj.list_tasks(
                    status=status,
                    agent=BRAIN_CLI_AGENT_LABEL,
                    limit=500,
                )
                or []
            )
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
            if _retry_not_due(task):
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


def _safe_defer(task_queue_obj: Any, task_id: str, error: str) -> None:
    try:
        retry_after = _retry_delay_for_dispatch_error(task_queue_obj, error)
        task_queue_obj.defer_task(
            task_id,
            error=error[:500],
            retry_after_s=retry_after,
            by="review_task_dispatcher",
        )
    except Exception as exc:
        log.debug("defer_task fallback raised for %s: %s", task_id, exc)
        _safe_fail(task_queue_obj, task_id, f"defer_failed:{str(exc)[:200]}; original={error[:200]}")


def _record_attempt_start(task_queue_obj: Any, task: dict, message: str) -> dict | None:
    try:
        return task_queue_obj.record_dispatch_attempt_start(
            task["id"],
            agent=BRAIN_CLI_AGENT_LABEL,
            backend="cli_llm",
            prompt_chars=len(message),
            metadata={
                "source": "review_task_dispatcher",
                "task_created_by": task.get("created_by", ""),
                "openclaw_fallback_allowed": False,
            },
        )
    except Exception as exc:
        log.debug("record_dispatch_attempt_start failed for %s: %s", task.get("id"), exc)
        return None


def _finish_attempt(task_queue_obj: Any, attempt: dict | None, **kwargs: Any) -> None:
    if not attempt:
        return
    try:
        task_queue_obj.finish_dispatch_attempt(attempt["id"], **kwargs)
    except Exception as exc:
        log.debug("finish_dispatch_attempt failed for %s: %s", attempt.get("id"), exc)


def _is_transient_dispatch_error(task_queue_obj: Any, error: str) -> bool:
    fn = getattr(task_queue_obj, "_is_transient_dispatch_error", None)
    if callable(fn):
        try:
            return bool(fn(error))
        except Exception as exc:
            log.debug("transient-dispatch classifier failed: %s", exc)
    err = (error or "").lower()
    return any(
        marker in err
        for marker in (
            "breaker_",
            "backend_cooldown",
            "probe_in_flight",
            "cli slots busy",
            "timeout",
            "rate limit",
            "rate_limit",
            "rate-limited",
            "quota",
            "overloaded",
        )
    )


def _retry_delay_for_dispatch_error(task_queue_obj: Any, error: str) -> int:
    fn = getattr(task_queue_obj, "_retry_delay_for_dispatch_error", None)
    if callable(fn):
        try:
            return int(fn(error))
        except Exception as exc:
            log.debug("retry-delay classifier failed: %s", exc)
    return 600


def _retry_not_due(task: dict) -> bool:
    meta = task.get("metadata") or {}
    if not isinstance(meta, dict):
        return False
    raw = meta.get("next_attempt_at")
    if not raw:
        return False
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt > datetime.now(UTC)


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
