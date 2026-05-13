"""Autonomy / tasks / goals / focus / agent messaging / triggers / quiet
hours / denylist / eval proposals / atoms introspection — the agency layer.

All endpoints are thin wrappers over brain_core helper modules.
"""

from __future__ import annotations

from typing import Annotated

from api_deps import _log_failure, _safe_http_detail, verify_bearer
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as PathParam
from pydantic import BaseModel, Field

router = APIRouter(dependencies=[Depends(verify_bearer)])


# ── Pydantic models ─────────────────────────────────────
class AutopilotRequest(BaseModel):
    enabled: bool
    confidence_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    updated_by: str = Field(default="api", max_length=32)


class TaskCreateRequest(BaseModel):
    title: str = Field(..., min_length=3, max_length=200)
    description: str = Field(default="", max_length=5000)
    assigned_agent: str | None = None
    priority: int = Field(default=5, ge=1, le=10)
    parent_goal_id: str | None = None
    confidence: float | None = None
    brain_recommendation: str = Field(default="", max_length=2000)
    metadata: dict = Field(default_factory=dict)


class CompleteTaskRequest(BaseModel):
    result: str = Field(default="", max_length=10000)


class GoalCreateRequest(BaseModel):
    title: str = Field(..., min_length=3, max_length=200)
    description: str = Field(default="", max_length=5000)
    auto_decompose: bool = True


class FocusRequest(BaseModel):
    content: str = Field(..., min_length=3, max_length=500)
    category: str = Field(default="focus")
    agent: str | None = None
    expires_hours: int = Field(default=168, ge=1, le=720)


class AgentMessageRequest(BaseModel):
    from_agent: str = Field(..., max_length=32)
    to_agent: str = Field(..., max_length=32)
    content: str = Field(..., min_length=1, max_length=5000)
    message_type: str = Field(default="info", max_length=32)
    priority: int = Field(default=5, ge=1, le=10)
    parent_task_id: str | None = None


class TriggerCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    condition_type: str = Field(..., max_length=50)
    condition_config: dict = Field(default_factory=dict)
    action_template: dict = Field(default_factory=dict)
    enabled: bool = True
    cooldown_seconds: int = Field(default=3600, ge=0, le=86400 * 7)


class TriggerUpdateRequest(BaseModel):
    description: str | None = None
    enabled: bool | None = None
    cooldown_seconds: int | None = Field(default=None, ge=0, le=86400 * 7)
    condition_config: dict | None = None
    action_template: dict | None = None


class QuietHoursRequest(BaseModel):
    start: str = Field(..., pattern=r"^\d{2}:\d{2}$")
    end: str = Field(..., pattern=r"^\d{2}:\d{2}$")
    tz: str = Field(default="America/Los_Angeles", max_length=64)
    exceptions: list[str] = Field(default_factory=list)


class DenylistEntryRequest(BaseModel):
    prefix: str = Field(..., min_length=1, max_length=100)


class EvalProposalCreateRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    expected: str = Field(..., min_length=1, max_length=2000)
    expected_sources: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    source_event: str = Field(default="manual", max_length=64)


class DecisionOutcomeRequest(BaseModel):
    actual_outcome: str = Field(..., min_length=1, max_length=2000)
    outcome_status: str = Field(..., pattern=r"^(pending|succeeded|failed|overridden)$")
    review_status: str | None = Field(
        default=None,
        pattern=r"^(unreviewed|needs_review|reviewed|accepted)$",
    )


class DecisionFeedbackTaskRequest(BaseModel):
    hours: int = Field(default=168, ge=1, le=24 * 90)
    min_failures: int = Field(default=2, ge=1, le=20)
    limit: int = Field(default=200, ge=1, le=1000)
    max_tasks: int = Field(default=5, ge=1, le=20)


class OverridePatternsTaskRequest(BaseModel):
    hours: int = Field(default=168, ge=1, le=24 * 90)
    min_overrides: int = Field(default=2, ge=1, le=20)
    limit: int = Field(default=500, ge=1, le=5000)
    max_tasks: int = Field(default=5, ge=1, le=20)


# ── Autonomy ───────────────────────────────────────────
@router.get("/brain/autopilot", tags=["autonomy"])
def get_autopilot() -> dict:
    try:
        from brain_core.autopilot import get_state

        return get_state()
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.get("/brain/state", tags=["autonomy"])
def get_brain_state(limit: int = Query(default=10, ge=1, le=50)) -> dict:
    """Read-only deterministic belief-state snapshot.

    This endpoint does not call an LLM. It compiles existing atom/task/outcome
    signals into a transparent agency surface for hooks, agents, and operators.
    """
    try:
        from brain_core.belief_state import build_belief_state

        return build_belief_state(limit=limit)
    except Exception as e:
        _log_failure(str(e), route="/brain/state")
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.get("/brain/decisions", tags=["autonomy"])
def list_brain_decisions(
    limit: int = Query(default=50, ge=1, le=200),
    outcome_status: str | None = Query(default=None),
    review_status: str | None = Query(default=None),
) -> dict:
    try:
        from brain_core.decision_ledger import list_decisions

        decisions = list_decisions(
            limit=limit,
            outcome_status=outcome_status,
            review_status=review_status,
        )
        return {"decisions": decisions, "total": len(decisions)}
    except Exception as e:
        _log_failure(str(e), route="/brain/decisions")
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.get("/brain/decisions/feedback", tags=["autonomy"])
def get_brain_decision_feedback(
    hours: int = Query(default=168, ge=1, le=24 * 90),
    min_failures: int = Query(default=2, ge=1, le=20),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict:
    """Read-only decision outcome feedback loop.

    Returns deterministic learning candidates from failed/overridden decisions.
    No policy mutation and no LLM call happen on this endpoint.
    """
    try:
        from brain_core.decision_ledger import decision_feedback_report

        return decision_feedback_report(hours=hours, min_failures=min_failures, limit=limit)
    except Exception as e:
        _log_failure(str(e), route="/brain/decisions/feedback")
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.post("/brain/decisions/feedback/tasks", tags=["autonomy"])
def create_brain_decision_feedback_tasks(req: DecisionFeedbackTaskRequest) -> dict:
    """Create deduped review tasks for decision feedback candidates.

    This endpoint creates review tasks only; it does not mutate memory policy
    or autonomy thresholds.
    """
    try:
        from brain_core.decision_ledger import create_feedback_review_tasks

        return create_feedback_review_tasks(
            hours=req.hours,
            min_failures=req.min_failures,
            limit=req.limit,
            max_tasks=req.max_tasks,
        )
    except Exception as e:
        _log_failure(str(e), route="/brain/decisions/feedback/tasks")
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.get("/brain/outcomes/feedback", tags=["autonomy"])
def get_brain_outcome_feedback(
    hours: int = Query(default=168, ge=1, le=24 * 90),
    min_overrides: int = Query(default=2, ge=1, le=20),
    limit: int = Query(default=500, ge=1, le=5000),
) -> dict:
    """chris_override pattern report from the task_queue outcomes table.

    Twin of /brain/decisions/feedback for the outcomes table — surfaces
    repeated overrides that decision_ledger never saw because the brain
    didn't initiate the underlying decision.
    """
    try:
        from brain_core.outcome_feedback import override_patterns_report

        return override_patterns_report(hours=hours, min_overrides=min_overrides, limit=limit)
    except Exception as e:
        _log_failure(str(e), route="/brain/outcomes/feedback")
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.post("/brain/outcomes/feedback/tasks", tags=["autonomy"])
def create_brain_outcome_feedback_tasks(req: OverridePatternsTaskRequest) -> dict:
    """Materialize override patterns into bounded review tasks.

    Deduplicated by signature so repeated runs do not spawn duplicates.
    Read-only with respect to autonomy policy.
    """
    try:
        from brain_core.outcome_feedback import create_override_review_tasks

        return create_override_review_tasks(
            hours=req.hours,
            min_overrides=req.min_overrides,
            limit=req.limit,
            max_tasks=req.max_tasks,
        )
    except Exception as e:
        _log_failure(str(e), route="/brain/outcomes/feedback/tasks")
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.get("/brain/trend-alerts", tags=["autonomy"])
def get_brain_trend_alerts() -> dict:
    """7d drift alerts on the tracked brain-quality metric vector.

    Returns an empty list when fewer than 2 daily snapshots are present —
    the metric_trend_snapshot job seeds history at 4:38am daily.
    """
    try:
        from brain_core.metric_trend_tracker import compute_trend_alerts

        return {"alerts": compute_trend_alerts()}
    except Exception as e:
        _log_failure(str(e), route="/brain/trend-alerts")
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.get("/brain/recall/wrong-rate-breakdown", tags=["autonomy"])
def get_brain_recall_wrong_rate_breakdown(
    hours: int = Query(default=168, ge=1, le=24 * 30),
) -> dict:
    """Per-slice wrong-rate breakdown for /recall judged outcomes.

    Slices: language (ko/en heuristic), route (/recall/v2 vs /recall/active),
    actor. Identifies the single worst slice (>=5 samples) to focus
    remediation work on.
    """
    try:
        from brain_core.recall_wrong_rate_breakdown import breakdown

        return breakdown(hours=hours)
    except Exception as e:
        _log_failure(str(e), route="/brain/recall/wrong-rate-breakdown")
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.post("/brain/decisions/{decision_id}/outcome", tags=["autonomy"])
def update_brain_decision_outcome(
    decision_id: Annotated[str, PathParam()],
    req: DecisionOutcomeRequest,
) -> dict:
    try:
        from brain_core.decision_ledger import update_decision_outcome

        updated = update_decision_outcome(
            decision_id,
            actual_outcome=req.actual_outcome,
            outcome_status=req.outcome_status,
            review_status=req.review_status,
        )
        if not updated:
            raise HTTPException(status_code=404, detail=f"decision '{decision_id}' not found")
        return {"status": "updated", "id": decision_id}
    except HTTPException:
        raise
    except Exception as e:
        _log_failure(str(e), route="/brain/decisions/outcome")
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.post("/brain/autopilot", tags=["autonomy"])
def set_autopilot(req: AutopilotRequest) -> dict:
    try:
        from brain_core.autopilot import set_state

        state = set_state(req.enabled, req.confidence_threshold, req.updated_by)
        if not req.enabled:
            try:
                from brain_core.task_queue import task_queue

                state["paused_tasks"] = task_queue.pause_running_tasks()
            except Exception:  # noqa: S110 — best-effort cleanup
                pass
        return state
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.post("/brain/tasks", tags=["autonomy"])
def create_task(req: TaskCreateRequest) -> dict:
    try:
        from brain_core.task_queue import task_queue

        agent = req.assigned_agent
        if not agent:
            try:
                from brain_core.reasoning import suggest_delegation

                agent = suggest_delegation(req.title + " " + req.description).get("agent", "jenna")
            except Exception:
                agent = "jenna"
        confidence = req.confidence if req.confidence is not None else 0.5
        return task_queue.create_task(
            title=req.title,
            description=req.description,
            assigned_agent=agent,
            priority=req.priority,
            parent_goal_id=req.parent_goal_id,
            confidence=confidence,
            confidence_reasoning=req.brain_recommendation,
            created_by="api",
            metadata=req.metadata,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.get("/brain/tasks", tags=["autonomy"])
def list_tasks(
    status: str | None = None,
    agent: str | None = None,
    goal: str | None = None,
    limit: int = 50,
) -> dict:
    try:
        from brain_core.task_queue import task_queue

        tasks = task_queue.list_tasks(status=status, agent=agent, parent_goal_id=goal, limit=limit)
        return {"tasks": tasks, "total": len(tasks)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.post("/brain/tasks/process", tags=["autonomy"])
def process_pending_tasks() -> dict:
    try:
        from brain_core.autopilot import get_state, is_enabled
        from brain_core.task_queue import task_queue

        state = get_state()
        if not is_enabled():
            return {"approved": [], "autopilot_enabled": False, "message": "autopilot is off"}
        approved, escalated = task_queue.process_pending()
        return {
            "approved": [
                {"id": t["id"], "title": t["title"], "confidence": t["confidence"]} for t in approved
            ],
            "escalated": [
                {"id": t["id"], "title": t["title"], "confidence": t["confidence"]} for t in escalated
            ],
            "total_approved": len(approved),
            "total_escalated": len(escalated),
            "autopilot_enabled": True,
            "confidence_threshold": state["confidence_threshold"],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.post("/brain/tasks/dispatch", tags=["autonomy"])
def dispatch_ready_tasks() -> dict:
    try:
        from brain_core.autopilot import is_enabled
        from brain_core.task_queue import task_queue

        if not is_enabled():
            return {"dispatched": [], "autopilot_enabled": False, "message": "autopilot is off"}
        results = task_queue.process_ready()
        return {
            "dispatched": [
                {
                    "id": t.get("id"),
                    "title": t.get("title"),
                    "status": t.get("status"),
                    "agent": t.get("assigned_agent"),
                }
                for t in results
            ],
            "total_dispatched": len(results),
            "autopilot_enabled": True,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.get("/brain/tasks/{task_id}", tags=["autonomy"])
def get_task(task_id: str) -> dict:
    try:
        from brain_core.task_queue import task_queue

        task = task_queue.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="task not found")
        return task
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.get("/brain/tasks/{task_id}/execution", tags=["autonomy"])
def get_task_execution_truth(task_id: str) -> dict:
    try:
        from brain_core.task_queue import task_queue

        return task_queue.get_task_execution_truth(task_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=_safe_http_detail("not_found", e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.get("/brain/task-dispatch-attempts", tags=["autonomy"])
def list_task_dispatch_attempts(
    task_id: str | None = None,
    trace_id: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    try:
        from brain_core.task_queue import task_queue

        attempts = task_queue.list_dispatch_attempts(
            task_id=task_id,
            trace_id=trace_id,
            status=status,
            limit=limit,
        )
        return {"attempts": attempts, "total": len(attempts)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.post("/brain/tasks/{task_id}/approve", tags=["autonomy"])
def approve_task(task_id: str) -> dict:
    try:
        from brain_core.task_queue import task_queue

        return task_queue.approve_task(task_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=_safe_http_detail("internal", e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.post("/brain/tasks/{task_id}/start", tags=["autonomy"])
def start_task(task_id: str) -> dict:
    try:
        from brain_core.task_queue import task_queue

        return task_queue.start_task(task_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=_safe_http_detail("internal", e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.post("/brain/tasks/{task_id}/complete", tags=["autonomy"])
def complete_task_route(
    task_id: str,
    result: str = "",
    chris_acked: bool = False,
    body: CompleteTaskRequest | None = None,
) -> dict:
    if not result and body is not None:
        result = body.result or ""
    try:
        from brain_core.task_queue import task_queue

        task = task_queue.get_task(task_id)
        updated = task_queue.complete_task(task_id, result=result)
        if chris_acked:
            try:
                domain = (task.get("metadata") or {}).get("domain", "general") if task else "general"
                task_queue.record_outcome(
                    task_id=task_id,
                    domain=domain,
                    brain_recommendation=task.get("confidence_reasoning", "") if task else "",
                    actual_action=result[:500],
                    chris_override=False,
                )
            except Exception:  # noqa: S110 — outcome record is best-effort
                pass
        return updated
    except ValueError as e:
        raise HTTPException(status_code=409, detail=_safe_http_detail("internal", e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.post("/brain/tasks/{task_id}/reject", tags=["autonomy"])
def reject_task(task_id: str) -> dict:
    try:
        from brain_core.task_queue import task_queue

        task = task_queue.get_task(task_id)
        updated = task_queue.fail_task(task_id, error="rejected by Chris")
        try:
            domain = (task.get("metadata") or {}).get("domain", "general") if task else "general"
            task_queue.record_outcome(
                task_id=task_id,
                domain=domain,
                brain_recommendation=task.get("confidence_reasoning", "") if task else "",
                actual_action="rejected by Chris",
                chris_override=True,
                override_reason="manual rejection",
            )
        except Exception:  # noqa: S110 — outcome record is best-effort
            pass
        return updated
    except ValueError as e:
        raise HTTPException(status_code=409, detail=_safe_http_detail("internal", e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


# ── Goals ──────────────────────────────────────────────
@router.post("/brain/goals", tags=["autonomy"])
def create_goal(req: GoalCreateRequest) -> dict:
    try:
        from brain_core.task_queue import task_queue

        goal = task_queue.create_goal(title=req.title, description=req.description)
        if req.auto_decompose:
            try:
                from brain_core.goal_decompose import decompose_goal

                goal["subtasks"] = decompose_goal(goal["id"])
            except Exception as exc:
                _log_failure(f"auto-decompose failed for {goal['id']}: {exc}", route="/brain/goals")
                goal["decompose_error"] = str(exc)[:200]
        return goal
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.get("/brain/goals", tags=["autonomy"])
def list_goals(status: str | None = None) -> dict:
    try:
        from brain_core.task_queue import task_queue

        goals = task_queue.list_goals(status=status)
        return {"goals": goals, "total": len(goals)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.post("/brain/goals/{goal_id}/complete", tags=["autonomy"])
def complete_goal_route(goal_id: str) -> dict:
    try:
        from brain_core.task_queue import task_queue

        return task_queue.complete_goal(goal_id, by="chris")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=_safe_http_detail("internal", e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.get("/brain/goals/{goal_id}", tags=["autonomy"])
def get_goal(goal_id: str) -> dict:
    try:
        from brain_core.task_queue import task_queue

        goal = task_queue.get_goal(goal_id)
        if not goal:
            raise HTTPException(status_code=404, detail="goal not found")
        goal["progress"] = task_queue.get_goal_progress(goal_id)
        goal["subtasks"] = task_queue.list_tasks(parent_goal_id=goal_id)
        return goal
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.post("/brain/goals/{goal_id}/decompose", tags=["autonomy"])
def decompose_goal_endpoint(goal_id: str) -> dict:
    try:
        from brain_core.goal_decompose import decompose_goal
        from brain_core.task_queue import task_queue

        if not task_queue.get_goal(goal_id):
            raise HTTPException(status_code=404, detail="goal not found")
        tasks = decompose_goal(goal_id)
        return {"goal_id": goal_id, "subtasks_created": len(tasks), "tasks": tasks}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


# ── Deprecated alias ───────────────────────────────────
@router.post("/brain/message", tags=["autonomy"], include_in_schema=False)
def send_message_legacy(req: AgentMessageRequest) -> dict:
    try:
        from brain_core.agent_messenger import send_message

        return send_message(
            req.from_agent,
            req.to_agent,
            req.content,
            req.message_type,
            req.priority,
            req.parent_task_id,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


# ── Focus ──────────────────────────────────────────────
@router.get("/brain/focus", tags=["autonomy"])
def get_focus() -> dict:
    try:
        from brain_core.working_memory import get_working_context

        return get_working_context()
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.post("/brain/focus", tags=["autonomy"])
def add_focus_route(req: FocusRequest) -> dict:
    try:
        from brain_core.working_memory import add_focus

        return add_focus(req.content, req.category, req.agent, req.expires_hours)
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.delete("/brain/focus/{focus_id}", tags=["autonomy"])
def delete_focus(focus_id: str) -> dict:
    try:
        from brain_core.working_memory import remove_focus

        ok = remove_focus(focus_id)
        return {"status": "removed" if ok else "not_found", "id": focus_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


# ── Phase D1: Agent messaging ─────────────────────────
@router.post("/brain/messages", tags=["coordination"])
def send_agent_message(req: AgentMessageRequest) -> dict:
    try:
        from brain_core.agent_messenger import send_message

        return send_message(
            from_agent=req.from_agent,
            to_agent=req.to_agent,
            content=req.content,
            message_type=req.message_type,
            priority=req.priority,
            parent_task_id=req.parent_task_id,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.get("/brain/messages/{agent}", tags=["coordination"])
def get_agent_messages(
    agent: Annotated[str, PathParam()],
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    try:
        from brain_core.agent_messenger import get_pending_messages

        messages = get_pending_messages(agent, limit=limit)
        return {"agent": agent, "total": len(messages), "messages": messages}
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.post("/brain/messages/{msg_id}/ack", tags=["coordination"])
def ack_agent_message(msg_id: Annotated[str, PathParam()]) -> dict:
    try:
        from brain_core.agent_messenger import deliver_message

        result = deliver_message(msg_id)
        if not result or (isinstance(result, dict) and result.get("error") == "not_found"):
            raise HTTPException(status_code=404, detail=f"message {msg_id} not found")
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.post("/brain/messages/{agent}/dismiss_all", tags=["coordination"])
def dismiss_all_messages(agent: Annotated[str, PathParam()]) -> dict:
    try:
        from brain_core.agent_messenger import dismiss_all

        count = dismiss_all(agent)
        return {"agent": agent, "dismissed": count}
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


# ── Phase B1: Triggers ─────────────────────────────────
@router.get("/brain/triggers", tags=["autonomy"])
def list_triggers_endpoint() -> dict:
    try:
        from brain_core.action_triggers import list_triggers

        triggers = list_triggers()
        return {"items": triggers, "total": len(triggers), "triggers": triggers}
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.post("/brain/triggers", tags=["autonomy"])
def create_trigger_endpoint(req: TriggerCreateRequest) -> dict:
    try:
        from brain_core.action_triggers import create_trigger

        return create_trigger(
            name=req.name,
            description=req.description,
            condition_type=req.condition_type,
            condition_config=req.condition_config,
            action_template=req.action_template,
            enabled=req.enabled,
            cooldown_seconds=req.cooldown_seconds,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=_safe_http_detail("internal", e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.patch("/brain/triggers/{trigger_id}", tags=["autonomy"])
def update_trigger_endpoint(trigger_id: str, req: TriggerUpdateRequest) -> dict:
    try:
        from brain_core.action_triggers import update_trigger

        result = update_trigger(
            trigger_id,
            description=req.description,
            enabled=req.enabled,
            cooldown_seconds=req.cooldown_seconds,
            condition_config=req.condition_config,
            action_template=req.action_template,
        )
        if result is None:
            raise HTTPException(status_code=404, detail="trigger not found")
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.delete("/brain/triggers/{trigger_id}", tags=["autonomy"])
def delete_trigger_endpoint(trigger_id: str) -> dict:
    try:
        from brain_core.action_triggers import delete_trigger

        ok = delete_trigger(trigger_id)
        if not ok:
            raise HTTPException(status_code=404, detail="trigger not found")
        return {"status": "deleted", "id": trigger_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


# ── Phase B2: Quiet hours ──────────────────────────────
def _quiet_hours_from_config() -> dict:
    import contextlib

    try:
        import json as _json
        import sqlite3

        from brain_core.config import AUTONOMY_DB
        from brain_core.default_levels import QUIET_HOURS

        conn = sqlite3.connect(str(AUTONOMY_DB))
        try:
            rows = conn.execute(
                "SELECT key, value FROM brain_config WHERE key LIKE 'quiet_hours.%'"
            ).fetchall()
        finally:
            conn.close()
        cfg = dict(QUIET_HOURS)
        for k, v in rows:
            short_key = k[len("quiet_hours.") :]
            if short_key == "exceptions":
                with contextlib.suppress(Exception):
                    cfg["exceptions"] = _json.loads(v)
            else:
                cfg[short_key] = v
        return cfg
    except Exception:
        from brain_core.default_levels import QUIET_HOURS

        return dict(QUIET_HOURS)


@router.get("/brain/quiet-hours", tags=["autonomy"])
def get_quiet_hours() -> dict:
    return _quiet_hours_from_config()


@router.post("/brain/quiet-hours", tags=["autonomy"])
def set_quiet_hours(req: QuietHoursRequest) -> dict:
    try:
        import json as _json

        from brain_core import brain_config_store
        from brain_core.autonomy import invalidate_levels_cache

        for k, v in (
            ("quiet_hours.start", req.start),
            ("quiet_hours.end", req.end),
            ("quiet_hours.tz", req.tz),
            ("quiet_hours.exceptions", _json.dumps(req.exceptions)),
        ):
            brain_config_store.set(k, v, updated_by="api")
        invalidate_levels_cache()
        return {"status": "set", **_quiet_hours_from_config()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


# ── Phase B3: Denylist ─────────────────────────────────
def _denylist_soft_from_config() -> list[str]:
    try:
        from brain_core import brain_config_store

        rows = brain_config_store.get_prefix("denylist.")
        return [k[len("denylist.") :] for k, v in rows.items() if v == "1"]
    except Exception:
        return []


@router.get("/brain/denylist", tags=["autonomy"])
def get_denylist() -> dict:
    from brain_core.default_levels import DENY_PREFIXES

    return {"hardcoded": list(DENY_PREFIXES), "soft": _denylist_soft_from_config()}


@router.post("/brain/denylist/add", tags=["autonomy"])
def add_denylist_entry(req: DenylistEntryRequest) -> dict:
    try:
        from brain_core import brain_config_store
        from brain_core.autonomy import invalidate_levels_cache

        brain_config_store.set(f"denylist.{req.prefix}", "1", updated_by="api")
        invalidate_levels_cache()
        return {"status": "added", "prefix": req.prefix}
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.post("/brain/denylist/remove", tags=["autonomy"])
def remove_denylist_entry(req: DenylistEntryRequest) -> dict:
    try:
        from brain_core import brain_config_store
        from brain_core.autonomy import invalidate_levels_cache

        removed = brain_config_store.delete(f"denylist.{req.prefix}")
        if not removed:
            raise HTTPException(status_code=404, detail="prefix not found in soft denylist")
        invalidate_levels_cache()
        return {"status": "removed", "prefix": req.prefix}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


# ── Phase B4: Eval proposals ───────────────────────────
@router.get("/brain/eval-proposals", tags=["eval"])
def list_eval_proposals(status: str = "candidate", limit: int = 50) -> dict:
    try:
        from brain_core.eval_proposals import list_candidates, stats

        return {"items": list_candidates(status=status, limit=limit), "stats": stats()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.post("/brain/eval-proposals", tags=["eval"])
def create_eval_proposal(req: EvalProposalCreateRequest) -> dict:
    try:
        from brain_core.eval_proposals import insert_proposal

        pid = insert_proposal(
            query=req.query,
            expected=req.expected,
            expected_sources=req.expected_sources,
            source_event=req.source_event,
            confidence=req.confidence,
        )
        if not pid:
            raise HTTPException(status_code=500, detail="insert returned no id")
        return {"status": "created", "id": pid}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.post("/brain/eval-proposals/{proposal_id}/approve", tags=["eval"])
def approve_eval_proposal(proposal_id: str) -> dict:
    try:
        from brain_core.eval_proposals import mark_status

        ok = mark_status(proposal_id, "promoted")
        if not ok:
            raise HTTPException(status_code=404, detail="proposal not found")
        return {"status": "promoted", "id": proposal_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.post("/brain/eval-proposals/{proposal_id}/reject", tags=["eval"])
def reject_eval_proposal(proposal_id: str) -> dict:
    try:
        from brain_core.eval_proposals import mark_status

        ok = mark_status(proposal_id, "rejected")
        if not ok:
            raise HTTPException(status_code=404, detail="proposal not found")
        return {"status": "rejected", "id": proposal_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.get("/brain/eval-proposals/stats", tags=["eval"])
def eval_proposal_stats() -> dict:
    try:
        from brain_core.eval_proposals import stats

        return stats()
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


# ── Phase B5: Atoms introspection ──────────────────────
@router.get("/brain/atoms/stats", tags=["atoms"])
def atoms_stats() -> dict:
    try:
        from brain_core.atoms_store import count_atoms

        return count_atoms()
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.get("/brain/atoms/{atom_id}/history", tags=["atoms"])
def get_atom_confidence_history(atom_id: str, limit: int = 50) -> dict:
    """Return the append-only atom_evidence ledger for an atom."""
    try:
        from brain_core.atoms_store import BRAIN_ATOMS_ENABLED, get_confidence_history

        if not BRAIN_ATOMS_ENABLED:
            raise HTTPException(status_code=503, detail="atoms not enabled")
        if limit < 1 or limit > 500:
            raise HTTPException(status_code=400, detail="limit must be 1-500")
        history = get_confidence_history(atom_id, limit=limit)
        return {"atom_id": atom_id, "count": len(history), "history": history}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.get("/brain/atoms/{atom_id}", tags=["atoms"])
def get_atom_detail(atom_id: str) -> dict:
    try:
        import sqlite3

        from brain_core.atoms_store import BRAIN_ATOMS_ENABLED, BRAIN_DB

        if not BRAIN_ATOMS_ENABLED:
            raise HTTPException(status_code=503, detail="atoms not enabled")
        conn = sqlite3.connect(str(BRAIN_DB))
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM atoms WHERE id = ?", (atom_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="atom not found")
            atom = dict(row)
            prov = conn.execute(
                "SELECT parent_kind, parent_id, child_kind, child_id, relation, confidence "
                "FROM provenance WHERE parent_id = ? OR child_id = ? LIMIT 50",
                (atom_id, atom_id),
            ).fetchall()
            atom["provenance"] = [dict(p) for p in prov]
        finally:
            conn.close()
        return atom
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.get("/brain/atoms", tags=["atoms"])
def list_atoms(
    tier: str | None = None,
    kind: str | None = None,
    canonical: int | None = None,
    limit: int = 50,
) -> dict:
    try:
        import sqlite3

        from brain_core.atoms_store import BRAIN_ATOMS_ENABLED, BRAIN_DB

        if not BRAIN_ATOMS_ENABLED:
            return {"items": [], "total": 0, "enabled": False}
        limit = max(1, min(500, limit))
        clauses = []
        params: list[object] = []
        if tier:
            clauses.append("tier = ?")
            params.append(tier)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if canonical is not None:
            clauses.append("canonical = ?")
            params.append(int(canonical))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        conn = sqlite3.connect(str(BRAIN_DB))
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        try:
            # `where` is composed from static column names only (tier/kind/canonical)
            cols = (
                "id, text, kind, tier, canonical, confidence, "
                "reinforcement_count, interval_days, easiness_factor, "
                "next_review_at, chroma_id, distilled_by, valid_from, valid_until, "
                "quality_score, created_at"
            )
            query = f"SELECT {cols} FROM atoms{where} ORDER BY created_at DESC LIMIT ?"  # noqa: S608
            rows = conn.execute(query, [*params, limit]).fetchall()
        finally:
            conn.close()
        return {"items": [dict(r) for r in rows], "total": len(rows), "enabled": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e
