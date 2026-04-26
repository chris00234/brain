from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "brain_core"))

from decision_ledger import (  # noqa: E402
    create_feedback_review_tasks,
    decision_feedback_report,
    list_decisions,
    record_decision,
    resolve_task_decisions,
    update_decision_outcome,
)
from task_queue import TaskQueue  # noqa: E402


def test_decision_ledger_records_and_lists_decision(tmp_path):
    db_path = tmp_path / "autonomy.db"

    decision_id = record_decision(
        actor="brain_loop",
        domain="brain",
        source="test",
        observation_kind="contradiction",
        observation_subject="c1",
        perceived_state={"summary": {"beliefs": 2}},
        candidate_options=[{"option": "dispatch_agent"}, {"option": "observe"}],
        selected_option="dispatch_agent",
        selected_payload={"agent": "sage"},
        confidence=0.7,
        autonomy_level="L2",
        expected_outcome="Sage resolves contradiction.",
        actual_outcome='{"status":"dispatched"}',
        outcome_status="succeeded",
        action_audit_id=123,
        db_path=db_path,
    )

    rows = list_decisions(db_path=db_path)

    assert rows[0]["id"] == decision_id
    assert rows[0]["observation_kind"] == "contradiction"
    assert rows[0]["perceived_state"]["summary"]["beliefs"] == 2
    assert rows[0]["candidate_options"][0]["option"] == "dispatch_agent"
    assert rows[0]["selected_payload"]["agent"] == "sage"
    assert rows[0]["action_audit_id"] == 123


def test_decision_ledger_updates_outcome(tmp_path):
    db_path = tmp_path / "autonomy.db"
    decision_id = record_decision(selected_option="observe", db_path=db_path)

    updated = update_decision_outcome(
        decision_id,
        actual_outcome="Chris accepted the recommendation.",
        outcome_status="succeeded",
        review_status="accepted",
        db_path=db_path,
    )
    rows = list_decisions(db_path=db_path)

    assert updated is True
    assert rows[0]["actual_outcome"] == "Chris accepted the recommendation."
    assert rows[0]["outcome_status"] == "succeeded"
    assert rows[0]["review_status"] == "accepted"
    assert rows[0]["resolved_at"] is not None


def test_decision_ledger_filters_pending_review(tmp_path):
    db_path = tmp_path / "autonomy.db"
    record_decision(selected_option="observe", outcome_status="pending", db_path=db_path)
    record_decision(
        selected_option="dispatch_agent",
        outcome_status="failed",
        review_status="needs_review",
        db_path=db_path,
    )

    rows = list_decisions(outcome_status="failed", review_status="needs_review", db_path=db_path)

    assert len(rows) == 1
    assert rows[0]["selected_option"] == "dispatch_agent"


def test_decision_ledger_dedupes_recent_identical_decision(tmp_path):
    db_path = tmp_path / "autonomy.db"

    first = record_decision(
        source="brain_decide",
        observation_kind="preference_decision",
        observation_subject="same-input",
        selected_option="option-a",
        dedupe_window_seconds=300,
        db_path=db_path,
    )
    second = record_decision(
        source="brain_decide",
        observation_kind="preference_decision",
        observation_subject="same-input",
        selected_option="option-a",
        dedupe_window_seconds=300,
        db_path=db_path,
    )
    different = record_decision(
        source="brain_decide",
        observation_kind="preference_decision",
        observation_subject="same-input",
        selected_option="option-b",
        dedupe_window_seconds=300,
        db_path=db_path,
    )

    rows = list_decisions(db_path=db_path)

    assert second == first
    assert different != first
    assert len(rows) == 2


def test_resolve_task_decisions_updates_pending_subject_match(tmp_path):
    db_path = tmp_path / "autonomy.db"
    record_decision(
        observation_subject="task_123",
        selected_option="dispatch",
        outcome_status="pending",
        db_path=db_path,
    )
    record_decision(
        observation_subject="task_done",
        selected_option="dispatch",
        outcome_status="succeeded",
        db_path=db_path,
    )

    updated = resolve_task_decisions(
        "task_123",
        actual_outcome="agent completed task",
        success=True,
        db_path=db_path,
    )
    rows = list_decisions(db_path=db_path)
    resolved = next(row for row in rows if row["observation_subject"] == "task_123")
    pre_resolved = next(row for row in rows if row["observation_subject"] == "task_done")

    assert updated == 1
    assert resolved["outcome_status"] == "succeeded"
    assert resolved["review_status"] == "accepted"
    assert resolved["actual_outcome"] == "agent completed task"
    assert pre_resolved["outcome_status"] == "succeeded"


def test_resolve_task_decisions_matches_task_id_in_payload(tmp_path):
    db_path = tmp_path / "autonomy.db"
    record_decision(
        observation_subject="different",
        selected_payload={"task_id": "task_456", "agent": "liz"},
        selected_option="dispatch",
        outcome_status="pending",
        db_path=db_path,
    )

    updated = resolve_task_decisions(
        "task_456",
        actual_outcome="agent failed",
        success=False,
        db_path=db_path,
    )
    rows = list_decisions(db_path=db_path)

    assert updated == 1
    assert rows[0]["outcome_status"] == "failed"
    assert rows[0]["review_status"] == "needs_review"


def test_resolve_task_decisions_does_not_match_task_id_substrings(tmp_path):
    db_path = tmp_path / "autonomy.db"
    record_decision(
        observation_subject="different",
        selected_payload={"task_id": "task_10"},
        perceived_state={"note": "task_1 appears inside longer text"},
        candidate_options=[{"id": "task_100"}],
        selected_option="dispatch",
        outcome_status="pending",
        db_path=db_path,
    )

    updated = resolve_task_decisions(
        "task_1",
        actual_outcome="should not resolve partial matches",
        success=False,
        db_path=db_path,
    )
    rows = list_decisions(db_path=db_path)

    assert updated == 0
    assert rows[0]["outcome_status"] == "pending"
    assert rows[0]["actual_outcome"] == ""


def test_decision_feedback_report_promotes_repeated_failures_without_llm(tmp_path):
    db_path = tmp_path / "autonomy.db"
    for idx in range(2):
        record_decision(
            domain="brain",
            source="brain_decide",
            observation_kind="preference_decision",
            observation_subject=f"case-{idx}",
            selected_option="new_daemon",
            confidence=0.86,
            actual_outcome="Chris rejected standing resource load",
            outcome_status="failed",
            review_status="needs_review",
            db_path=db_path,
        )
    record_decision(
        domain="brain",
        source="brain_decide",
        observation_kind="preference_decision",
        observation_subject="ok-case",
        selected_option="bounded_hook",
        confidence=0.72,
        outcome_status="succeeded",
        review_status="accepted",
        db_path=db_path,
    )

    report = decision_feedback_report(db_path=db_path)

    assert report["contract"]["uses_llm"] is False
    assert report["contract"]["mutates_policy"] is False
    assert report["summary"]["by_outcome_status"]["failed"] == 2
    assert len(report["learning_candidates"]) == 1
    candidate = report["learning_candidates"][0]
    assert candidate["pattern"]["selected_option"] == "new_daemon"
    assert candidate["failed"] == 2
    assert "lower_confidence_for_this_pattern_until_reviewed" in candidate["recommended_actions"]
    assert "tighten_decide_context_or_option_framing" in candidate["recommended_actions"]
    assert len(report["pending_reviews"]) == 2


def test_create_feedback_review_tasks_is_deduped_and_review_only(tmp_path):
    db_path = tmp_path / "autonomy.db"
    tq = TaskQueue(db_path)
    for idx in range(2):
        record_decision(
            domain="brain",
            source="brain_loop",
            observation_kind="task_dispatch",
            observation_subject=f"task-{idx}",
            selected_option="dispatch_agent",
            confidence=0.9,
            actual_outcome="agent failed to complete safely",
            outcome_status="failed",
            review_status="needs_review",
            db_path=db_path,
        )

    first = create_feedback_review_tasks(db_path=db_path, task_queue_obj=tq, min_failures=2)
    second = create_feedback_review_tasks(db_path=db_path, task_queue_obj=tq, min_failures=2)
    tasks = tq.list_tasks(status="pending")

    assert len(first["created"]) == 1
    assert first["created"][0]["task_id"] == tasks[0]["id"]
    assert second["created"] == []
    assert second["skipped"][0]["reason"] == "open_task_exists"
    assert tasks[0]["created_by"] == "decision_feedback"
    assert tasks[0]["metadata"]["mutates_policy"] is False
    assert tasks[0]["metadata"]["uses_llm"] is False
    assert tasks[0]["metadata"]["decision_feedback_signature"]
