from __future__ import annotations

import sys
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))

from task_queue import TaskQueue  # noqa: E402


class _Gate:
    allowed = True
    requires_ack = False
    reason = ""


def test_create_task_sets_trace_id_from_handoff_metadata(tmp_path):
    tq = TaskQueue(tmp_path / "autonomy.db")

    task = tq.create_task(
        title="Handoff from jenna",
        description="Check dispatch truth.",
        assigned_agent="ellie",
        metadata={"source_message_id": "msg_123"},
    )

    assert task["metadata"]["trace_id"] == "msg_123"


def test_process_ready_records_completed_dispatch_attempt(monkeypatch, tmp_path):
    class _Result:
        ok = True
        text = "DONE: verified"
        error = ""
        backend = "codex"
        model = "gpt-5.5"
        attempts = 1
        duration_ms = 123
        rate_limited = False

        @property
        def provider(self):
            return "codex"

    dispatch_calls = []
    fake_cli = type(sys)("cli_llm")

    def fake_dispatch(**kwargs):
        dispatch_calls.append(kwargs)
        return _Result()

    fake_cli.dispatch = fake_dispatch
    fake_autonomy = type(sys)("autonomy")
    fake_autonomy.authorize = lambda *_args, **_kwargs: _Gate()
    monkeypatch.setitem(sys.modules, "cli_llm", fake_cli)
    monkeypatch.setitem(sys.modules, "autonomy", fake_autonomy)

    tq = TaskQueue(tmp_path / "autonomy.db")
    task = tq.create_task(
        title="Handoff from jenna",
        description="Check dispatch truth.",
        assigned_agent="ellie",
        metadata={"source_message_id": "msg_success"},
        confidence=0.9,
    )
    tq.approve_task(task["id"], by="test")

    tq.process_ready()
    truth = tq.get_task_execution_truth(task["id"])

    attempts = truth["dispatch_attempts"]
    assert len(attempts) == 1
    assert attempts[0]["trace_id"] == "msg_success"
    assert attempts[0]["status"] == "completed"
    assert attempts[0]["backend"] == "codex"
    assert attempts[0]["model"] == "gpt-5.5"
    assert dispatch_calls[0].get("backend") is None
    assert dispatch_calls[0].get("openclaw_agent") == "ellie"
    assert "max_backends" not in dispatch_calls[0]
    assert attempts[0]["response_chars"] == len("DONE: verified")
    assert truth["evidence"] == {
        "has_dispatch_attempt": True,
        "has_closed_dispatch_attempt": True,
        "has_outcome": True,
        "terminal_has_outcome": True,
    }


def test_process_ready_records_deferred_dispatch_attempt_without_outcome(monkeypatch, tmp_path):
    class _Result:
        ok = False
        text = ""
        error = "timeout after 120s: gateway slow"
        backend = "codex"
        model = "gpt-5.5"
        attempts = 1
        duration_ms = 120000
        rate_limited = False

    dispatch_calls = []
    fake_cli = type(sys)("cli_llm")

    def fake_dispatch(**kwargs):
        dispatch_calls.append(kwargs)
        return _Result()

    fake_cli.dispatch = fake_dispatch
    fake_autonomy = type(sys)("autonomy")
    fake_autonomy.authorize = lambda *_args, **_kwargs: _Gate()
    monkeypatch.setitem(sys.modules, "cli_llm", fake_cli)
    monkeypatch.setitem(sys.modules, "autonomy", fake_autonomy)

    tq = TaskQueue(tmp_path / "autonomy.db")
    task = tq.create_task(
        title="Handoff from jenna",
        description="Check dispatch truth.",
        assigned_agent="sage",
        confidence=0.9,
    )
    tq.approve_task(task["id"], by="test")

    tq.process_ready()
    updated = tq.get_task(task["id"])
    truth = tq.get_task_execution_truth(task["id"])

    assert updated["status"] == "approved"
    assert truth["dispatch_attempts"][0]["status"] == "deferred"
    assert truth["dispatch_attempts"][0]["error_class"] == "transient_dispatch"
    assert truth["evidence"]["has_outcome"] is False
    assert truth["evidence"]["terminal_has_outcome"] is True


def test_task_evaluation_human_needed_sends_action_summary(monkeypatch, tmp_path):
    class _Result:
        ok = True
        text = "HUMAN_NEEDED: account owner approval is required"
        error = ""

    fake_cli = type(sys)("cli_llm")
    fake_cli.dispatch = lambda **_kwargs: _Result()
    monkeypatch.setitem(sys.modules, "cli_llm", fake_cli)

    tq = TaskQueue(tmp_path / "autonomy.db")
    sent = []

    def fake_notify(body: str, *, source: str, severity: str = "warn") -> None:
        sent.append({"body": body, "source": source, "severity": severity})

    monkeypatch.setattr(tq, "_notify_chris_text", fake_notify)
    task = tq.create_task(
        title="Needs account owner",
        description="Payment account access required",
        assigned_agent="sage",
        confidence=0.2,
    )

    handled = tq._review_tasks_with_subscription_llm([task])
    updated = tq.get_task(task["id"])

    assert handled == {task["id"]}
    assert sent == [
        {
            "body": sent[0]["body"],
            "source": "task_queue:evaluation_action_summary",
            "severity": "info",
        }
    ]
    assert "TASK EVALUATION ACTION" in sent[0]["body"]
    assert "requires Chris" not in sent[0]["body"]
    assert "Chris input required" not in sent[0]["body"]
    assert updated["metadata"]["task_evaluation_alert_policy"] == "action_summary"
    assert updated["metadata"]["task_evaluation_action"] == "held_for_safe_followup"
    assert updated["metadata"]["task_evaluation_source"] == "llm_human_needed"


def test_policy_human_required_sends_action_summary_not_escalation_alert(monkeypatch, tmp_path):
    tq = TaskQueue(tmp_path / "autonomy.db")
    sent = []

    def fake_notify(body: str, *, source: str, severity: str = "warn") -> None:
        sent.append({"body": body, "source": source, "severity": severity})

    monkeypatch.setattr(tq, "_notify_chris_text", fake_notify)
    task = tq.create_task(
        title="Delete production data",
        description="Irreversible wipe requested",
        confidence=0.1,
        metadata={"requires_human": True},
    )

    tq._escalate_tasks([task])
    updated = tq.get_task(task["id"])

    assert sent[0]["source"] == "task_queue:evaluation_action_summary"
    assert sent[0]["severity"] == "info"
    assert "TASK EVALUATION ACTION" in sent[0]["body"]
    assert "requires Chris" not in sent[0]["body"]
    assert "Chris input required" not in sent[0]["body"]
    assert updated["metadata"]["task_evaluation_alert_policy"] == "action_summary"
    assert updated["metadata"]["task_evaluation_source"] == "classifier_human_required"


def test_brain_speak_urgent_handoff_routes_to_agent_execution(monkeypatch, tmp_path):
    dispatch_calls: list[dict] = []

    class _Result:
        ok = True
        text = "HANDLEABLE: Sage should resolve stale escalations from Brain evidence and report outcomes."
        error = ""

    fake_cli = type(sys)("cli_llm")
    fake_cli.dispatch = lambda **kwargs: dispatch_calls.append(kwargs) or _Result()
    monkeypatch.setitem(sys.modules, "cli_llm", fake_cli)

    tq = TaskQueue(tmp_path / "autonomy.db")
    task = tq.create_task(
        title=(
            "Handoff from brain_speak_urgent: Urgent Brain observation with no active CLI session. "
            "Handle it yourself if possible; notify Chris only for a true human blocker."
        ),
        description=(
            "[brain_speak_urgent] no active CLI sessions; urgent observations:\n"
            "- [7.5] stale_thread_drive/thread: claude → chris 메시지 108h째 pending: "
            "20 task escalations need your review"
        ),
        assigned_agent="sage",
        confidence=0.0,
    )

    tq._escalate_tasks([task])
    updated = tq.get_task(task["id"])

    assert len(dispatch_calls) == 1
    assert updated["status"] == "approved"
    assert updated["metadata"]["task_evaluation_alert_policy"] == "autonomous_log"
    assert updated["metadata"]["task_evaluation_decision"] == "handleable"
    assert updated["metadata"]["task_evaluation_action"] == "routed_for_agent_execution"
    assert updated["metadata"]["task_evaluation_source"] == "llm_handleable"


class _InlinePool:
    def submit(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)


class _NoopPool:
    def submit(self, fn, *args, **kwargs):
        return None


def test_deferred_dispatch_records_failure_lesson(monkeypatch, tmp_path):
    import task_queue as tq_module

    class _Result:
        ok = False
        text = ""
        error = "timeout after 120s: gateway slow"
        backend = "codex"
        model = "gpt-5.5"
        attempts = 1
        duration_ms = 120000
        rate_limited = False

    recorded: list[dict] = []
    fake_cli = type(sys)("cli_llm")
    fake_cli.dispatch = lambda **_kwargs: _Result()
    fake_autonomy = type(sys)("autonomy")
    fake_autonomy.authorize = lambda *_args, **_kwargs: _Gate()
    # task_queue routes transient/infra errors (timeout, backend_cooldown,
    # rate_limit, gateway) to record_infra_failure_lesson instead of the
    # general record_failure_lesson path; both surfaces must be stubbed so
    # whichever the dispatcher chooses captures the lesson record.
    fake_failure = type(sys)("failure_memory")
    fake_failure.record_failure_lesson = lambda **kwargs: recorded.append(kwargs) or "lesson_1"
    fake_failure.record_infra_failure_lesson = lambda task_description, failure_reason, agent_id: (
        recorded.append(
            {
                "task_description": task_description,
                "failure_reason": failure_reason,
                "agent_id": agent_id,
                "context": "transient_dispatch",
            }
        )
        or "lesson_1"
    )
    monkeypatch.setitem(sys.modules, "cli_llm", fake_cli)
    monkeypatch.setitem(sys.modules, "autonomy", fake_autonomy)
    monkeypatch.setitem(sys.modules, "failure_memory", fake_failure)
    monkeypatch.setattr(tq_module, "_bg_pool", _InlinePool())

    tq = TaskQueue(tmp_path / "autonomy.db")
    task = tq.create_task(
        title="Gateway handoff check",
        description="Verify Hermes profile gateway dispatch truth.",
        assigned_agent="sage",
        confidence=0.9,
    )
    tq.approve_task(task["id"], by="test")

    tq.process_ready()
    truth = tq.get_task_execution_truth(task["id"])

    assert len(recorded) == 1
    assert "Gateway handoff check" in recorded[0]["task_description"]
    assert recorded[0]["failure_reason"] == "timeout after 120s: gateway slow"
    assert recorded[0]["agent_id"] == "sage"
    assert "transient_dispatch" in recorded[0]["context"]
    assert truth["dispatch_attempts"][0]["metadata"]["failure_lesson_status"] == "recorded"
    assert truth["dispatch_attempts"][0]["metadata"]["failure_lesson_id"] == "lesson_1"
    assert truth["task"]["metadata"]["last_failure_lesson_status"] == "recorded"


def test_completed_task_clears_stale_dispatch_error(tmp_path):
    tq = TaskQueue(tmp_path / "autonomy.db")
    task = tq.create_task(
        title="Retry after backend cooldown",
        description="Complete successfully after an earlier transient defer.",
        confidence=0.9,
    )
    tq.approve_task(task["id"], by="test")
    tq.start_task(task["id"], by="test")
    deferred = tq.defer_task(task["id"], error="backend_cooldown 238s", by="test")
    assert deferred["status"] == "approved"
    assert deferred["error"] == "backend_cooldown 238s"

    tq.start_task(task["id"], by="test")
    completed = tq.complete_task(task["id"], result="DONE", by="test")

    assert completed["status"] == "completed"
    assert completed["result"] == "DONE"
    assert completed["error"] == ""


def test_retrieved_failure_lessons_are_injected_into_next_task(monkeypatch, tmp_path):
    import task_queue as tq_module

    class _Result:
        ok = True
        text = "DONE: avoided stale gateway path"
        error = ""
        backend = "codex"
        model = "gpt-5.5"
        attempts = 1
        duration_ms = 10
        rate_limited = False

        @property
        def provider(self):
            return "codex"

    dispatch_calls: list[dict] = []
    fake_cli = type(sys)("cli_llm")
    fake_cli.dispatch = lambda **kwargs: dispatch_calls.append(kwargs) or _Result()
    fake_autonomy = type(sys)("autonomy")
    fake_autonomy.authorize = lambda *_args, **_kwargs: _Gate()
    fake_failure = type(sys)("failure_memory")
    fake_failure.get_similar_lessons = lambda *args, **kwargs: [
        {
            "id": "lesson_gateway",
            "reflection": "Gateway dispatch timed out because the old route was unavailable.",
            "avoid": "Do not assume queued means executed.",
            "try_next": "Check dispatch truth and retry through CLI-first path.",
        }
    ]
    monkeypatch.setitem(sys.modules, "cli_llm", fake_cli)
    monkeypatch.setitem(sys.modules, "autonomy", fake_autonomy)
    monkeypatch.setitem(sys.modules, "failure_memory", fake_failure)
    monkeypatch.setattr(tq_module, "_bg_pool", _NoopPool())

    tq = TaskQueue(tmp_path / "autonomy.db")
    task = tq.create_task(
        title="Gateway handoff check",
        description="Retry Hermes profile gateway dispatch truth.",
        assigned_agent="sage",
        confidence=0.9,
    )
    tq.approve_task(task["id"], by="test")

    tq.process_ready()

    prompt = dispatch_calls[0]["message"]
    assert "Past failures to AVOID" in prompt
    assert "Do not assume queued means executed" in prompt
    assert "Check dispatch truth and retry through CLI-first path" in prompt
    truth = tq.get_task_execution_truth(task["id"])
    assert truth["task"]["metadata"]["retrieved_lesson_ids"] == ["lesson_gateway"]
    assert truth["dispatch_attempts"][0]["metadata"]["lesson_ids"] == ["lesson_gateway"]
    assert truth["outcomes"][0]["lesson_ids"] == ["lesson_gateway"]


def test_task_evaluation_handleable_records_visible_llm_action(monkeypatch, tmp_path):
    class _Result:
        ok = True
        text = "HANDLEABLE: assign Sage to inspect the logs and report concrete evidence"
        error = ""

    fake_cli = type(sys)("cli_llm")
    fake_cli.dispatch = lambda **_kwargs: _Result()
    monkeypatch.setitem(sys.modules, "cli_llm", fake_cli)

    tq = TaskQueue(tmp_path / "autonomy.db")
    task = tq.create_task(
        title="Inspect automation result",
        description="Find what Brain did automatically",
        assigned_agent="human",
        confidence=0.2,
    )

    handled = tq._review_tasks_with_subscription_llm([task])
    updated = tq.get_task(task["id"])

    assert handled == {task["id"]}
    assert updated["assigned_agent"] == "sage"
    assert updated["status"] == "approved"
    assert updated["metadata"]["task_evaluation_alert_policy"] == "autonomous_log"
    assert updated["metadata"]["task_evaluation_decision"] == "handleable"
    assert updated["metadata"]["task_evaluation_action"] == "routed_for_agent_execution"
    assert updated["metadata"]["task_evaluation_brain_action"] == "reassigned_to_sage_and_approved"
    assert updated["metadata"]["task_evaluation_source"] == "llm_handleable"
    assert updated["metadata"]["task_evaluation_next_evidence"] == f"/brain/tasks/{task['id']}/execution"
    assert any(row.get("event") == "task_evaluation" for row in updated["execution_log"])
