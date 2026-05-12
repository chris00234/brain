"""Behavioral unit tests for task_queue.TaskQueue.

Exercises the goal/outcome lifecycle on an isolated tmp_path autonomy.db:
  - Goal creation, listing, filtered listing, retrieval
  - Forward-only goal status transitions (active -> completed | cancelled)
  - Rejection of invalid status values and invalid transitions
  - Outcome recording (without a parent task — minimal columns)
  - Outcome listing with domain filter

These are the public surface used by routes/agency.py and the brain_loop.
Importing task_queue triggers heavy module init; tests use a fresh
TaskQueue(db_path=tmp_path/...) so the production autonomy.db is untouched.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


@pytest.fixture
def tq(tmp_path):
    from task_queue import TaskQueue

    return TaskQueue(db_path=tmp_path / "autonomy.db")


def test_create_goal_returns_active_record(tq):
    goal = tq.create_goal(title="Ship D11", description="next sprint")
    assert goal["id"].startswith("goal_")
    assert goal["title"] == "Ship D11"
    assert goal["status"] == "active"
    assert goal["created_by"] == "chris"


def test_get_goal_missing_returns_none(tq):
    assert tq.get_goal("goal_nonexistent") is None


def test_list_goals_returns_all_created(tq):
    """Goals are listed (newest-first by created_at, ties broken by SQLite).
    We assert membership + count, not strict order — _now() is second-precision
    so rapid creates collide and the tiebreaker is implementation-defined.
    """
    a = tq.create_goal(title="first")
    b = tq.create_goal(title="second")
    c = tq.create_goal(title="third")

    ids = {g["id"] for g in tq.list_goals()}
    assert ids == {a["id"], b["id"], c["id"]}


def test_list_goals_filters_by_status(tq):
    g1 = tq.create_goal(title="alive")
    g2 = tq.create_goal(title="done")
    tq.update_goal_status(g2["id"], "completed")

    active = tq.list_goals(status="active")
    completed = tq.list_goals(status="completed")

    assert g1["id"] in [g["id"] for g in active]
    assert g2["id"] not in [g["id"] for g in active]
    assert g2["id"] in [g["id"] for g in completed]


def test_update_goal_status_completes(tq):
    goal = tq.create_goal(title="will complete")
    updated = tq.update_goal_status(goal["id"], "completed", by="test")
    assert updated["status"] == "completed"
    assert updated["completed_at"] is not None


def test_update_goal_status_cancels(tq):
    goal = tq.create_goal(title="will cancel")
    updated = tq.update_goal_status(goal["id"], "cancelled", by="test")
    assert updated["status"] == "cancelled"
    assert updated["completed_at"] is not None


def test_update_goal_status_rejects_invalid_value(tq):
    goal = tq.create_goal(title="x")
    with pytest.raises(ValueError, match="invalid goal status"):
        tq.update_goal_status(goal["id"], "in_progress")


def test_update_goal_status_rejects_terminal_transition(tq):
    """Completed/cancelled are terminal — no further transitions allowed."""
    goal = tq.create_goal(title="terminal")
    tq.update_goal_status(goal["id"], "completed")
    with pytest.raises(ValueError, match="cannot transition"):
        tq.update_goal_status(goal["id"], "cancelled")
    with pytest.raises(ValueError, match="cannot transition"):
        tq.update_goal_status(goal["id"], "active")


def test_update_goal_status_unknown_goal(tq):
    with pytest.raises(ValueError, match="not found"):
        tq.update_goal_status("goal_ghost", "completed")


def test_record_outcome_writes_row(tq):
    tq.record_outcome(
        task_id="task_synthetic_1",
        domain="general",
        brain_recommendation="proceed",
        actual_action="proceeded",
        chris_override=False,
    )
    rows = tq.list_outcomes(domain="general")
    assert len(rows) == 1
    r = rows[0]
    assert r["task_id"] == "task_synthetic_1"
    assert r["brain_recommendation"] == "proceed"
    assert r["chris_override"] == 0


def test_record_outcome_override_increments_correctly(tq):
    """An override stamp must register as chris_override=1 and reflect in the listing."""
    tq.record_outcome(
        task_id="task_override_1",
        domain="coding",
        brain_recommendation="A",
        actual_action="B",
        chris_override=True,
        override_reason="prefers B",
    )
    rows = tq.list_outcomes(domain="coding")
    assert len(rows) == 1
    assert rows[0]["chris_override"] == 1
    assert rows[0]["override_reason"] == "prefers B"


def test_list_outcomes_domain_filter_isolates(tq):
    tq.record_outcome(task_id="t1", domain="general", chris_override=False)
    tq.record_outcome(task_id="t2", domain="coding", chris_override=True)
    tq.record_outcome(task_id="t3", domain="general", chris_override=False)

    general = tq.list_outcomes(domain="general")
    coding = tq.list_outcomes(domain="coding")
    all_rows = tq.list_outcomes()

    assert {r["task_id"] for r in general} == {"t1", "t3"}
    assert {r["task_id"] for r in coding} == {"t2"}
    assert len(all_rows) == 3
