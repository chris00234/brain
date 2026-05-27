"""Structural SLO fix: every failed dispatch must produce a lesson.

The `task_failure_lesson_missing_count` SLO breaches when a failed/deferred
attempt's metadata.failure_lesson_status != 'recorded'. Two regressions are
covered here:

  1. `harness_unregistered` errors ("Requested agent harness 'codex' is not
     registered" / "Unknown agent id") were being tagged `terminal_dispatch`
     and routed to the LLM-based recorder, which then deadlocked because the
     LLM call itself depended on the missing harness — producing
     status='record_failed' permanently.

  2. Even when the LLM path is reachable but the recorder returns no lesson
     (transient neo4j miss, schema mismatch), the SLO would still breach. The
     deterministic infra recorder is now a safety net so every failure
     produces at least the per-error-class deterministic lesson.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "brain_core"))

import failure_memory  # noqa: E402
from task_queue import TaskQueue, _record_failure_lesson_bg  # noqa: E402


def test_classify_harness_unregistered():
    err = 'GatewayClientRequestError: Error: Requested agent harness "codex" is not registered.'
    assert failure_memory._classify_infra_error(err) == "harness_unregistered"

    err = "Error: Unknown agent id \"brain_cli\". Use 'openclaw agents list' to see configured agents."
    assert failure_memory._classify_infra_error(err) == "harness_unregistered"


def test_classify_harness_unregistered_outranks_gateway():
    """Gateway substring is a superset; harness_unregistered must win."""
    err = 'GatewayClientRequestError: Requested agent harness "codex" is not registered'
    # Both "gateway" and "not registered" are present; the harness_unregistered
    # branch is checked first because the lesson text describes the correct fix.
    assert failure_memory._classify_infra_error(err) == "harness_unregistered"


def test_infra_reflection_has_harness_unregistered_entry():
    reflection, avoid, try_next = failure_memory._INFRA_REFLECTION["harness_unregistered"]
    assert "harness" in reflection.lower() or "registered" in reflection.lower()
    assert avoid
    assert try_next


def test_is_transient_dispatch_error_covers_harness_unregistered():
    is_transient = TaskQueue._is_transient_dispatch_error
    assert is_transient(
        'GatewayClientRequestError: Error: Requested agent harness "codex" is not registered.'
    )
    assert is_transient('Error: Unknown agent id "brain_cli".')
    assert is_transient("GatewayClientRequestError: connection reset")


def test_bg_lesson_falls_back_to_deterministic_when_llm_returns_none(monkeypatch, tmp_path):
    """The LLM recorder may return None (e.g. neo4j miss). Safety net catches it."""
    db_path = tmp_path / "autonomy.db"
    queue = TaskQueue(db_path)
    task = queue.create_task(
        "Dispatch harness check",
        description="Force a failed attempt with no LLM lesson.",
        confidence=0.9,
    )
    queue.approve_task(task["id"], by="test")
    attempt = queue.record_dispatch_attempt_start(task["id"], agent="jenna", backend="codex")
    queue.finish_dispatch_attempt(
        attempt["id"],
        status="failed",
        error_class="terminal_dispatch",
        error='Requested agent harness "codex" is not registered.',
    )
    queue._conn().commit()

    monkeypatch.setattr(failure_memory, "record_failure_lesson", lambda **kwargs: None)
    monkeypatch.setattr(
        failure_memory,
        "record_infra_failure_lesson",
        lambda task_description, failure_reason, agent_id: "lesson_infra_fallback_1",
    )

    _record_failure_lesson_bg(
        task_description="Dispatch harness check",
        failure_reason='Requested agent harness "codex" is not registered.',
        agent_id="jenna",
        context="status=failed; error_class=terminal_dispatch",
        db_path=db_path,
        attempt_id=attempt["id"],
        task_id=task["id"],
    )

    truth = queue.get_task_execution_truth(task["id"])
    meta = truth["dispatch_attempts"][0]["metadata"]
    assert meta["failure_lesson_status"] == "recorded"
    assert meta["failure_lesson_id"] == "lesson_infra_fallback_1"
    assert truth["task"]["metadata"]["last_failure_lesson_status"] == "recorded"


def test_bg_lesson_falls_back_when_llm_raises(monkeypatch, tmp_path):
    """Same fallback when the LLM recorder raises rather than returning None."""
    db_path = tmp_path / "autonomy.db"
    queue = TaskQueue(db_path)
    task = queue.create_task("Crash recorder", confidence=0.9)
    queue.approve_task(task["id"], by="test")
    attempt = queue.record_dispatch_attempt_start(task["id"], agent="sage", backend="codex")
    queue.finish_dispatch_attempt(
        attempt["id"],
        status="failed",
        error_class="terminal_dispatch",
        error='Unknown agent id "brain_cli"',
    )

    def _boom(**_kwargs):
        raise RuntimeError("neo4j gateway closed")

    monkeypatch.setattr(failure_memory, "record_failure_lesson", _boom)
    monkeypatch.setattr(
        failure_memory,
        "record_infra_failure_lesson",
        lambda task_description, failure_reason, agent_id: "lesson_after_crash",
    )

    _record_failure_lesson_bg(
        task_description="Crash recorder",
        failure_reason='Unknown agent id "brain_cli"',
        agent_id="sage",
        db_path=db_path,
        attempt_id=attempt["id"],
        task_id=task["id"],
    )

    truth = queue.get_task_execution_truth(task["id"])
    meta = truth["dispatch_attempts"][0]["metadata"]
    assert meta["failure_lesson_status"] == "recorded"
    assert meta["failure_lesson_id"] == "lesson_after_crash"


def test_bg_lesson_records_failed_when_both_paths_fail(monkeypatch, tmp_path):
    """If even the deterministic path fails, status stays record_failed —
    the SLO should still see it, not silently mark recorded."""
    db_path = tmp_path / "autonomy.db"
    queue = TaskQueue(db_path)
    task = queue.create_task("Both paths fail", confidence=0.9)
    queue.approve_task(task["id"], by="test")
    attempt = queue.record_dispatch_attempt_start(task["id"], agent="ellie", backend="codex")
    queue.finish_dispatch_attempt(
        attempt["id"], status="failed", error_class="terminal_dispatch", error="boom"
    )

    monkeypatch.setattr(failure_memory, "record_failure_lesson", lambda **_kw: None)
    monkeypatch.setattr(
        failure_memory,
        "record_infra_failure_lesson",
        lambda task_description, failure_reason, agent_id: None,
    )

    _record_failure_lesson_bg(
        task_description="Both paths fail",
        failure_reason="boom",
        agent_id="ellie",
        db_path=db_path,
        attempt_id=attempt["id"],
        task_id=task["id"],
    )

    truth = queue.get_task_execution_truth(task["id"])
    meta = truth["dispatch_attempts"][0]["metadata"]
    assert meta["failure_lesson_status"] == "record_failed"
    assert meta["failure_lesson_id"] == ""
