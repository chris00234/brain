from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "brain_core"))

from decision_ledger import list_decisions, record_decision  # noqa: E402
from task_queue import TaskQueue  # noqa: E402


def test_record_outcome_resolves_matching_pending_decision(tmp_path):
    db_path = tmp_path / "autonomy.db"
    queue = TaskQueue(db_path)
    task = queue.create_task("Verify brain decision loop", confidence=0.8)
    record_decision(
        source="brain_loop",
        observation_kind="task_dispatch",
        observation_subject=task["id"],
        selected_option="dispatch",
        outcome_status="pending",
        db_path=db_path,
    )

    queue.record_outcome(
        task["id"],
        domain="brain",
        brain_recommendation="dispatch was appropriate",
        actual_action="completed successfully",
        chris_override=False,
    )

    rows = list_decisions(db_path=db_path)
    assert rows[0]["outcome_status"] == "succeeded"
    assert rows[0]["review_status"] == "accepted"
    assert rows[0]["actual_outcome"] == "completed successfully"


def test_record_outcome_marks_failed_decision_needs_review(tmp_path):
    db_path = tmp_path / "autonomy.db"
    queue = TaskQueue(db_path)
    task = queue.create_task("Verify failed decision loop", confidence=0.8)
    record_decision(
        source="brain_loop",
        observation_kind="task_dispatch",
        observation_subject="different-subject",
        selected_payload={"task_id": task["id"]},
        selected_option="dispatch",
        outcome_status="pending",
        db_path=db_path,
    )

    queue.record_outcome(
        task["id"],
        domain="brain",
        brain_recommendation="dispatch was appropriate",
        actual_action="agent execution failed",
        chris_override=True,
        override_reason="agent execution failed",
    )

    rows = list_decisions(db_path=db_path)
    assert rows[0]["outcome_status"] == "failed"
    assert rows[0]["review_status"] == "needs_review"


def test_record_outcome_persists_retrieved_procedure_ids(tmp_path):
    db_path = tmp_path / "autonomy.db"
    queue = TaskQueue(db_path)
    task = queue.create_task(
        "Run known procedure",
        metadata={"retrieved_procedure_ids": ["proc_a", "proc_b"]},
    )

    queue.record_outcome(task["id"], actual_action="completed successfully")

    rows = queue.list_outcomes()
    assert rows[0]["procedure_ids"] == ["proc_a", "proc_b"]


def test_record_outcome_persists_retrieved_lesson_ids(tmp_path):
    db_path = tmp_path / "autonomy.db"
    queue = TaskQueue(db_path)
    task = queue.create_task(
        "Avoid known failure",
        metadata={"retrieved_lesson_ids": ["lesson_a", "lesson_b"]},
    )

    queue.record_outcome(task["id"], actual_action="completed successfully")

    rows = queue.list_outcomes()
    assert rows[0]["lesson_ids"] == ["lesson_a", "lesson_b"]
