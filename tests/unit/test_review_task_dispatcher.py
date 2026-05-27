"""tests/unit/test_review_task_dispatcher.py — cli_llm review-task pickup.

Locks the contract: oldest-first selection over brain-generated tasks
(filter on `created_by`, not on agent label), cap honoured, success →
completed / transient failure → deferred, no dispatcher invocation when zero
eligible tasks exist. The dispatcher uses `cli_llm.cli_dispatch`
(Codex gpt-5.5 primary) and accepts a `prompt=` keyword.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "brain_core"))

from review_task_dispatcher import dispatch_pending_review_tasks  # noqa: E402
from task_queue import TaskQueue  # noqa: E402


@dataclass
class _StubCliResult:
    ok: bool
    text: str = ""
    error: str = ""
    attempts: int = 1
    duration_ms: int = 100
    backend: str = "codex"
    model: str = "gpt-5.5"


def _seed_task(
    tq: TaskQueue,
    *,
    created_by: str,
    agent: str = "brain_cli",
    title: str = "",
) -> dict:
    goal = tq.create_goal("Brain self-quality", "test")
    return tq.create_task(
        title=title or f"Review task ({created_by})",
        description=f"Description for {created_by}",
        assigned_agent=agent,
        priority=3,
        parent_goal_id=goal["id"],
        created_by=created_by,
        metadata={"source": created_by, "domain": "brain-system"},
    )


def test_dispatcher_completes_successful_cli_run(tmp_path):
    tq = TaskQueue(tmp_path / "autonomy.db")
    task = _seed_task(tq, created_by="outcome_feedback")

    calls: list[dict] = []

    def fake_dispatch(**kwargs):
        calls.append(kwargs)
        return _StubCliResult(ok=True, text="OK from cli")

    result = dispatch_pending_review_tasks(max_dispatches=2, task_queue_obj=tq, dispatch_fn=fake_dispatch)
    assert len(result["dispatched"]) == 1
    assert result["dispatched"][0]["task_id"] == task["id"]
    # cli_dispatch contract: positional-less, prompt=, timeout=
    assert "prompt" in calls[0]
    assert "outcome_feedback" in calls[0]["prompt"]
    assert "agent" not in calls[0]
    assert calls[0]["allow_openclaw_fallback"] is False
    assert tq.get_task(task["id"])["status"] == "completed"
    attempts = tq.list_dispatch_attempts(task_id=task["id"])
    assert attempts[0]["status"] == "completed"
    assert attempts[0]["metadata"]["openclaw_fallback_allowed"] is False


def test_dispatcher_defers_task_on_transient_cli_error(tmp_path):
    tq = TaskQueue(tmp_path / "autonomy.db")
    task = _seed_task(tq, created_by="goal_subtask_scaffold")

    def fake_dispatch(**_kwargs):
        return _StubCliResult(ok=False, error="rate-limited")

    result = dispatch_pending_review_tasks(max_dispatches=2, task_queue_obj=tq, dispatch_fn=fake_dispatch)
    assert result["dispatched"] == []
    assert result["skipped"][0]["task_id"] == task["id"]
    assert result["skipped"][0]["reason"] == "cli_dispatch_deferred"
    stored = tq.get_task(task["id"])
    assert stored["status"] == "approved"
    assert stored["metadata"]["next_attempt_at"]
    attempts = tq.list_dispatch_attempts(task_id=task["id"])
    assert attempts[0]["status"] == "deferred"


def test_dispatcher_fails_task_on_terminal_cli_error(tmp_path):
    tq = TaskQueue(tmp_path / "autonomy.db")
    task = _seed_task(tq, created_by="goal_subtask_scaffold")

    def fake_dispatch(**_kwargs):
        return _StubCliResult(ok=False, error="invalid task prompt")

    result = dispatch_pending_review_tasks(max_dispatches=2, task_queue_obj=tq, dispatch_fn=fake_dispatch)
    assert result["dispatched"] == []
    assert result["skipped"][0]["task_id"] == task["id"]
    assert result["skipped"][0]["reason"] == "cli_dispatch_failed"
    assert tq.get_task(task["id"])["status"] == "failed"
    attempts = tq.list_dispatch_attempts(task_id=task["id"])
    assert attempts[0]["status"] == "failed"


def test_dispatcher_filters_non_review_created_by(tmp_path):
    tq = TaskQueue(tmp_path / "autonomy.db")
    _ = _seed_task(tq, created_by="manual")

    called: list[dict] = []

    def fake_dispatch(**kwargs):
        called.append(kwargs)
        return _StubCliResult(ok=True)

    result = dispatch_pending_review_tasks(max_dispatches=2, task_queue_obj=tq, dispatch_fn=fake_dispatch)
    assert called == []
    assert result["dispatched"] == []
    assert result.get("reason") == "no_eligible_tasks"


def test_dispatcher_respects_cap_and_oldest_first(tmp_path):
    tq = TaskQueue(tmp_path / "autonomy.db")
    # 3 candidates; cap = 2; oldest two should be dispatched.
    a = _seed_task(tq, created_by="outcome_feedback", title="first")
    b = _seed_task(tq, created_by="outcome_feedback", title="second")
    c = _seed_task(tq, created_by="outcome_feedback", title="third")
    _ = c

    def fake_dispatch(**_kwargs):
        return _StubCliResult(ok=True, text="ok")

    result = dispatch_pending_review_tasks(max_dispatches=2, task_queue_obj=tq, dispatch_fn=fake_dispatch)
    dispatched_ids = {d["task_id"] for d in result["dispatched"]}
    assert dispatched_ids == {a["id"], b["id"]}
    assert tq.get_task(c["id"])["status"] == "pending"


def test_dispatcher_records_backend_and_model_in_result(tmp_path):
    tq = TaskQueue(tmp_path / "autonomy.db")
    _seed_task(tq, created_by="outcome_feedback")

    def fake_dispatch(**_kwargs):
        return _StubCliResult(ok=True, text="ok", backend="codex", model="gpt-5.5")

    result = dispatch_pending_review_tasks(max_dispatches=2, task_queue_obj=tq, dispatch_fn=fake_dispatch)
    assert result["dispatched"][0]["backend"] == "codex"
    assert result["dispatched"][0]["model"] == "gpt-5.5"


def test_dispatcher_queries_brain_cli_agent_beyond_default_limit(tmp_path):
    tq = TaskQueue(tmp_path / "autonomy.db")
    for i in range(60):
        _seed_task(tq, created_by="manual", agent="other", title=f"manual {i}")
    target = _seed_task(tq, created_by="outcome_feedback", agent="brain_cli", title="eligible")

    def fake_dispatch(**_kwargs):
        return _StubCliResult(ok=True, text="ok")

    result = dispatch_pending_review_tasks(max_dispatches=2, task_queue_obj=tq, dispatch_fn=fake_dispatch)

    assert [d["task_id"] for d in result["dispatched"]] == [target["id"]]


# ---------------------------------------------------------------------------
# Failure-lesson recording regression coverage (task_failure_lesson_missing_count SLO).
# Each failure path must invoke `_record_failure_lesson_async` with the attempt id
# so dispatch_attempt metadata gets `failure_lesson_status` populated; otherwise
# the SLO breaches every time a review task fails.
# ---------------------------------------------------------------------------


def _patch_lesson_recorder(tq: TaskQueue) -> list[dict]:
    """Replace `_record_failure_lesson_async` with a capture spy."""
    calls: list[dict] = []

    def spy(task, failure_reason, agent_id, *, context="", attempt_id=""):
        calls.append(
            {
                "task_id": task.get("id"),
                "failure_reason": failure_reason,
                "agent_id": agent_id,
                "context": context,
                "attempt_id": attempt_id,
            }
        )

    tq._record_failure_lesson_async = spy  # type: ignore[method-assign]
    return calls


def test_dispatcher_records_lesson_on_transient_cli_failure(tmp_path):
    tq = TaskQueue(tmp_path / "autonomy.db")
    task = _seed_task(tq, created_by="outcome_feedback")
    lesson_calls = _patch_lesson_recorder(tq)

    def fake_dispatch(**_kwargs):
        return _StubCliResult(ok=False, error="rate-limited")

    dispatch_pending_review_tasks(max_dispatches=2, task_queue_obj=tq, dispatch_fn=fake_dispatch)

    assert len(lesson_calls) == 1, "transient failure must invoke failure-lesson recorder"
    call = lesson_calls[0]
    assert call["task_id"] == task["id"]
    assert call["agent_id"] == "brain_cli"
    assert "transient_dispatch" in call["context"]
    assert call["failure_reason"] == "rate-limited"
    attempts = tq.list_dispatch_attempts(task_id=task["id"])
    assert call["attempt_id"] == attempts[0]["id"]


def test_dispatcher_records_lesson_on_terminal_cli_failure(tmp_path):
    tq = TaskQueue(tmp_path / "autonomy.db")
    task = _seed_task(tq, created_by="outcome_feedback")
    lesson_calls = _patch_lesson_recorder(tq)

    def fake_dispatch(**_kwargs):
        return _StubCliResult(ok=False, error="invalid task prompt")

    dispatch_pending_review_tasks(max_dispatches=2, task_queue_obj=tq, dispatch_fn=fake_dispatch)

    assert len(lesson_calls) == 1, "terminal failure must invoke failure-lesson recorder"
    call = lesson_calls[0]
    assert call["task_id"] == task["id"]
    assert call["agent_id"] == "brain_cli"
    assert "terminal_dispatch" in call["context"]
    assert call["failure_reason"] == "invalid task prompt"
    attempts = tq.list_dispatch_attempts(task_id=task["id"])
    assert call["attempt_id"] == attempts[0]["id"]


def test_dispatcher_records_lesson_on_dispatch_exception(tmp_path):
    tq = TaskQueue(tmp_path / "autonomy.db")
    task = _seed_task(tq, created_by="outcome_feedback")
    lesson_calls = _patch_lesson_recorder(tq)

    def fake_dispatch(**_kwargs):
        raise RuntimeError("boom")

    dispatch_pending_review_tasks(max_dispatches=2, task_queue_obj=tq, dispatch_fn=fake_dispatch)

    assert len(lesson_calls) == 1, "dispatch exception must invoke failure-lesson recorder"
    call = lesson_calls[0]
    assert call["task_id"] == task["id"]
    assert call["agent_id"] == "brain_cli"
    assert "dispatch_exception" in call["context"]
    assert "boom" in call["failure_reason"]
    attempts = tq.list_dispatch_attempts(task_id=task["id"])
    assert call["attempt_id"] == attempts[0]["id"]


def test_dispatcher_records_lesson_on_complete_task_failure(tmp_path):
    tq = TaskQueue(tmp_path / "autonomy.db")
    task = _seed_task(tq, created_by="outcome_feedback")
    lesson_calls = _patch_lesson_recorder(tq)

    def explode(*_a, **_kw):
        raise RuntimeError("complete kaboom")

    tq.complete_task = explode  # type: ignore[method-assign]

    def fake_dispatch(**_kwargs):
        return _StubCliResult(ok=True, text="OK from cli")

    dispatch_pending_review_tasks(max_dispatches=2, task_queue_obj=tq, dispatch_fn=fake_dispatch)

    assert len(lesson_calls) == 1, "complete_failed branch must invoke failure-lesson recorder"
    call = lesson_calls[0]
    assert call["task_id"] == task["id"]
    assert call["agent_id"] == "brain_cli"
    assert "complete_failed" in call["context"]
    assert "complete kaboom" in call["failure_reason"]
    attempts = tq.list_dispatch_attempts(task_id=task["id"])
    assert call["attempt_id"] == attempts[0]["id"]


def test_dispatcher_does_not_record_lesson_on_success(tmp_path):
    tq = TaskQueue(tmp_path / "autonomy.db")
    _seed_task(tq, created_by="outcome_feedback")
    lesson_calls = _patch_lesson_recorder(tq)

    def fake_dispatch(**_kwargs):
        return _StubCliResult(ok=True, text="OK from cli")

    dispatch_pending_review_tasks(max_dispatches=2, task_queue_obj=tq, dispatch_fn=fake_dispatch)

    assert lesson_calls == [], "successful dispatch must not invoke the failure-lesson recorder"
