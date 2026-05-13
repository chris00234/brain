"""tests/unit/test_review_task_dispatcher.py — cli_llm review-task pickup.

Locks the contract: oldest-first selection over brain-generated tasks
(filter on `created_by`, not on agent label), cap honoured, success →
completed / failure → failed, no dispatcher invocation when zero
eligible tasks exist. The dispatcher uses `cli_llm.cli_dispatch`
(codex → claude fallback) and accepts a `prompt=` keyword.
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
    assert tq.get_task(task["id"])["status"] == "completed"


def test_dispatcher_fails_task_on_cli_error(tmp_path):
    tq = TaskQueue(tmp_path / "autonomy.db")
    task = _seed_task(tq, created_by="goal_subtask_scaffold")

    def fake_dispatch(**_kwargs):
        return _StubCliResult(ok=False, error="rate-limited")

    result = dispatch_pending_review_tasks(max_dispatches=2, task_queue_obj=tq, dispatch_fn=fake_dispatch)
    assert result["dispatched"] == []
    assert result["skipped"][0]["task_id"] == task["id"]
    assert result["skipped"][0]["reason"] == "cli_dispatch_failed"
    assert tq.get_task(task["id"])["status"] == "failed"


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
        return _StubCliResult(ok=True, text="ok", backend="claude", model="claude-opus-4-7")

    result = dispatch_pending_review_tasks(max_dispatches=2, task_queue_obj=tq, dispatch_fn=fake_dispatch)
    assert result["dispatched"][0]["backend"] == "claude"
    assert result["dispatched"][0]["model"] == "claude-opus-4-7"
