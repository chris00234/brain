from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "brain_core"))

import failure_lesson_audit  # noqa: E402
from failure_lesson_audit import failure_lesson_outcome_snapshot  # noqa: E402
from task_queue import TaskQueue  # noqa: E402


def test_failure_lesson_outcome_snapshot_counts_linked_outcomes(tmp_path):
    db_path = tmp_path / "autonomy.db"
    queue = TaskQueue(db_path)
    for idx in range(5):
        task = queue.create_task(
            f"Task {idx}",
            metadata={"retrieved_lesson_ids": ["lesson_gateway"]},
            confidence=0.8,
        )
        queue.record_outcome(
            task["id"],
            actual_action="completed" if idx < 4 else "failed",
            chris_override=idx >= 4,
            override_reason="agent execution failed" if idx >= 4 else "",
        )

    out = failure_lesson_outcome_snapshot(db_path)

    assert out["status"] == "ok"
    assert out["linked_outcomes"] == 5
    assert out["linked_success"] == 4
    assert out["linked_failure"] == 1
    assert out["success_rate"] == 0.8
    assert out["lessons_with_outcomes"] == 1
    assert out["readiness_blocking"] is False


def test_failure_lesson_outcome_snapshot_is_insufficient_before_minimum(tmp_path):
    db_path = tmp_path / "autonomy.db"
    queue = TaskQueue(db_path)
    task = queue.create_task("Task", metadata={"retrieved_lesson_ids": ["lesson_a"]})
    queue.record_outcome(task["id"], actual_action="completed")

    out = failure_lesson_outcome_snapshot(db_path)

    assert out["status"] == "insufficient_data"
    assert out["linked_outcomes"] == 1
    assert out["readiness_blocking"] is True


def test_failure_lesson_outcome_snapshot_nonblocking_when_no_lessons_exist(tmp_path, monkeypatch):
    db_path = tmp_path / "autonomy.db"
    TaskQueue(db_path)
    monkeypatch.setattr(failure_lesson_audit, "_active_lesson_count", lambda: 0)

    out = failure_lesson_outcome_snapshot(db_path)

    assert out["status"] == "no_lessons"
    assert out["linked_outcomes"] == 0
    assert out["active_lessons"] == 0
    assert out["readiness_blocking"] is False


def test_failure_lesson_outcome_snapshot_blocks_missing_schema(tmp_path):
    db_path = tmp_path / "autonomy.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE outcomes (id TEXT PRIMARY KEY, task_id TEXT, chris_override INTEGER)")

    out = failure_lesson_outcome_snapshot(db_path)

    assert out["status"] == "blocked"
    assert out["readiness_blocking"] is True
    assert "lesson_ids" in out["reason"]
