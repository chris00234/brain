from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core"))

import agent_messenger
import escalation_policy
from task_queue import TaskQueue


def test_escalation_policy_defaults_handleable_to_llm():
    route = escalation_policy.classify_escalation(
        title="Debug failing recall test",
        content="Investigate why rerank output changed and propose a fix.",
    )

    assert route.target == "llm"
    assert route.notify_human is False


def test_escalation_policy_keeps_debuggable_login_issues_with_llm():
    route = escalation_policy.classify_escalation(
        title="Debug login regression",
        content="Investigate the auth callback failure and propose a patch.",
    )

    assert route.target == "llm"
    assert route.notify_human is False


def test_escalation_policy_detects_human_only_knowledge():
    route = escalation_policy.classify_escalation(
        title="Production login blocked",
        content="Requires 2FA code before continuing.",
    )

    assert route.target == "human"
    assert route.reason == "credential_or_account_blocker"


def test_escalation_policy_routes_knowledge_gaps_to_agents_first():
    route = escalation_policy.classify_escalation(
        title="Knowledge gap: TurboTax 결제 확인 메일 어디 있지?",
        content="Find or ingest a source that answers it.",
        metadata={"gap_query": "TurboTax 결제 확인 메일 어디 있지?"},
    )

    assert route.target == "llm"
    assert route.reason == "knowledge_gap_agent_remediation"


def test_escalation_policy_identifies_silent_personal_factoid_gaps():
    examples = [
        ("Knowledge gap: Chris shoe size sneaker size foot size", "Chris must provide this personal fact."),
        ("Knowledge gap: what is my birthday?", "Chris must provide his date of birth."),
        ("Knowledge gap: 내 주소가 뭐야?", "Chris must provide the private address."),
        ("Knowledge gap: Who was my first grade teacher?", "Chris must provide this childhood fact."),
    ]

    for title, reason in examples:
        assert escalation_policy.should_silence_personal_factoid_gap(
            title=title,
            content="Recall returned no reliable source.",
            metadata={},
            llm_reason=f"HUMAN_NEEDED: {reason}",
        )


def test_escalation_policy_does_not_silence_remediable_or_credential_gaps():
    assert not escalation_policy.should_silence_personal_factoid_gap(
        title="Knowledge gap: TurboTax 결제 확인 메일 어디 있지?",
        content="Find or ingest a source that answers it.",
        metadata={"gap_query": "TurboTax 결제 확인 메일 어디 있지?"},
        llm_reason="HUMAN_NEEDED: Chris must provide missing context.",
    )
    assert not escalation_policy.should_silence_personal_factoid_gap(
        title="Knowledge gap: GitHub account 2FA recovery code",
        content="Account login blocked.",
        metadata={},
        llm_reason="HUMAN_NEEDED: Chris must provide a 2FA code for account access.",
    )


def test_escalation_policy_keeps_brain_speak_urgent_wrapper_with_agents():
    route = escalation_policy.classify_escalation(
        title=(
            "Handoff from brain_speak_urgent: Urgent Brain observation with no active CLI session. "
            "Handle it yourself if possible; notify Chris only for a true human blocker."
        ),
        content=(
            "[brain_speak_urgent] no active CLI sessions; urgent observations:\n"
            "- [7.5] stale_thread_drive/thread: claude → chris 메시지 108h째 pending: "
            "20 task escalations need your review"
        ),
    )

    assert route.target == "llm"
    assert route.notify_human is False


def test_escalation_policy_keeps_truncated_brain_speak_urgent_wrapper_with_agents():
    route = escalation_policy.classify_escalation(
        title=(
            "Handoff from brain_speak_urgent: Urgent Brain observation with no active CLI session. "
            "Handle it yourself if possible; notify Chris only for a true human "
        ),
        content="",
        metadata={"task_evaluation_reason": "explicit_human_request"},
    )

    assert route.target == "llm"
    assert route.notify_human is False


def test_agent_messenger_decision_uses_llm_before_notifying(monkeypatch):
    llm_calls: list[dict] = []
    telegram_calls: list[dict] = []

    class _Result:
        ok = True
        text = "HANDLEABLE: Sage can inspect the evidence and recommend next action."

    def _dispatch(**kwargs):
        llm_calls.append(kwargs)
        return _Result()

    fake_cli = type(sys)("cli_llm")
    fake_cli.dispatch = _dispatch
    fake_telegram = type(sys)("telegram_alert")
    fake_telegram.send_chris_telegram = lambda **kwargs: telegram_calls.append(kwargs) or True
    monkeypatch.setitem(sys.modules, "cli_llm", fake_cli)
    monkeypatch.setitem(sys.modules, "telegram_alert", fake_telegram)

    action = agent_messenger.route_message(
        {
            "from_agent": "liz",
            "to_agent": "sage",
            "content": "Decide the best refactor path for duplicated tests.",
            "message_type": "decision",
            "priority": 2,
            "metadata": {},
        }
    )

    assert action == "forwarded"
    assert len(llm_calls) == 1
    assert telegram_calls == []


def test_agent_messenger_notifies_for_explicit_human_blocker(monkeypatch):
    llm_calls: list[dict] = []
    telegram_calls: list[dict] = []

    fake_cli = type(sys)("cli_llm")
    fake_cli.dispatch = lambda **kwargs: llm_calls.append(kwargs)
    fake_telegram = type(sys)("telegram_alert")
    fake_telegram.send_chris_telegram = lambda *args, **kwargs: telegram_calls.append(kwargs) or True
    monkeypatch.setitem(sys.modules, "cli_llm", fake_cli)
    monkeypatch.setitem(sys.modules, "telegram_alert", fake_telegram)

    action = agent_messenger.route_message(
        {
            "from_agent": "ellie",
            "to_agent": "jenna",
            "content": "Alert: need Chris login and 2FA code for the account owner.",
            "message_type": "alert",
            "priority": 1,
            "metadata": {},
        }
    )

    assert action == "escalated"
    assert llm_calls == []
    assert len(telegram_calls) == 1


def test_agent_messenger_handoff_task_keeps_full_description(monkeypatch):
    created: list[dict] = []

    class _FakeTaskQueue:
        def create_task(self, **kwargs):
            created.append(kwargs)
            return {"id": "task_fake", **kwargs}

    fake_task_queue = type(sys)("task_queue")
    fake_task_queue.task_queue = _FakeTaskQueue()
    monkeypatch.setitem(sys.modules, "task_queue", fake_task_queue)

    content = "Resolve/confirm the full handoff details, including repo paths and expected evidence."
    action = agent_messenger.route_message(
        {
            "id": "msg_123",
            "from_agent": "jenna",
            "to_agent": "ellie",
            "content": content,
            "message_type": "handoff",
            "priority": 4,
            "metadata": {},
        }
    )

    assert action == "task_created"
    assert created[0]["description"] == content
    assert created[0]["metadata"]["source"] == "agent_messenger"
    assert created[0]["metadata"]["source_via"] == "agent_messenger"
    assert created[0]["metadata"]["source_message_id"] == "msg_123"
    assert created[0]["metadata"]["from_agent"] == "jenna"


def test_agent_messenger_handoff_task_preserves_upstream_source(monkeypatch):
    created: list[dict] = []

    class _FakeTaskQueue:
        def create_task(self, **kwargs):
            created.append(kwargs)
            return {"id": "task_fake", **kwargs}

    fake_task_queue = type(sys)("task_queue")
    fake_task_queue.task_queue = _FakeTaskQueue()
    monkeypatch.setitem(sys.modules, "task_queue", fake_task_queue)

    agent_messenger.route_message(
        {
            "id": "msg_urgent",
            "from_agent": "brain_speak_urgent",
            "to_agent": "sage",
            "content": "Urgent Brain observation with no active CLI session.",
            "message_type": "handoff",
            "priority": 2,
            "metadata": {"source": "brain_speak_urgent"},
        }
    )

    assert created[0]["description"] == "Urgent Brain observation with no active CLI session."
    assert created[0]["metadata"]["source"] == "brain_speak_urgent"
    assert created[0]["metadata"]["source_via"] == "agent_messenger"
    assert created[0]["metadata"]["source_message_id"] == "msg_urgent"


def test_task_queue_reviews_handleable_escalation_with_subscription_llm(monkeypatch, tmp_path):
    llm_calls: list[dict] = []
    telegram_calls: list[dict] = []

    class _Result:
        ok = True
        text = "HANDLEABLE: Assigned agent can inspect logs and retry."

    fake_cli = type(sys)("cli_llm")
    fake_cli.dispatch = lambda **kwargs: llm_calls.append(kwargs) or _Result()
    fake_telegram = type(sys)("telegram_alert")
    fake_telegram.send_chris_telegram = lambda *args, **kwargs: telegram_calls.append(kwargs) or True
    monkeypatch.setitem(sys.modules, "cli_llm", fake_cli)
    monkeypatch.setitem(sys.modules, "telegram_alert", fake_telegram)

    tq = TaskQueue(tmp_path / "autonomy.db")
    tq._escalate_tasks(
        [
            {
                "id": "task_handleable",
                "title": "Investigate recall regression",
                "description": "Find the failing query path and propose a patch.",
                "confidence": 0.35,
                "assigned_agent": "sage",
                "metadata": {},
            }
        ]
    )

    assert len(llm_calls) == 1
    assert telegram_calls == []


def test_task_queue_self_routes_handleable_real_task(monkeypatch, tmp_path):
    class _Result:
        ok = True
        text = "HANDLEABLE: Sage can inspect local sources and resolve the task."

    fake_cli = type(sys)("cli_llm")
    fake_cli.dispatch = lambda **_kwargs: _Result()
    fake_telegram = type(sys)("telegram_alert")
    fake_telegram.send_chris_telegram = lambda *args, **kwargs: True
    monkeypatch.setitem(sys.modules, "cli_llm", fake_cli)
    monkeypatch.setitem(sys.modules, "telegram_alert", fake_telegram)

    tq = TaskQueue(tmp_path / "autonomy.db")
    task = tq.create_task(
        "Handoff from claude: stale task review",
        "Review and resolve without Chris if possible.",
        assigned_agent="chris",
        confidence=0.0,
    )

    handled = tq._review_tasks_with_subscription_llm([task])
    updated = tq.get_task(task["id"])

    assert handled == {task["id"]}
    assert updated["status"] == "approved"
    assert updated["assigned_agent"] == "sage"
    assert updated["metadata"]["escalation_llm_route"] == "handleable"


def test_task_queue_defers_transient_dispatch_errors(monkeypatch, tmp_path):
    dispatch_calls: list[dict] = []

    class _Gate:
        allowed = True
        requires_ack = False
        reason = ""

    class _Result:
        ok = False
        text = ""
        error = "breaker_half_open_probing (cooldown 0s)"

    fake_cli = type(sys)("cli_llm")
    fake_cli.dispatch = lambda **kwargs: dispatch_calls.append(kwargs) or _Result()
    fake_autonomy = type(sys)("autonomy")
    fake_autonomy.authorize = lambda *_args, **_kwargs: _Gate()
    monkeypatch.setitem(sys.modules, "cli_llm", fake_cli)
    monkeypatch.setitem(sys.modules, "autonomy", fake_autonomy)

    tq = TaskQueue(tmp_path / "autonomy.db")
    task = tq.create_task(
        title="Handoff from jenna",
        description="Verify a brain knowledge gap.",
        assigned_agent="ellie",
        confidence=0.9,
    )
    tq.approve_task(task["id"], by="test")

    results = tq.process_ready()
    updated = tq.get_task(task["id"])

    assert len(results) == 1
    assert dispatch_calls[0].get("backend") is None
    assert dispatch_calls[0].get("openclaw_agent") == "ellie"
    assert "max_backends" not in dispatch_calls[0]
    assert dispatch_calls[0]["agent"] == "ellie"
    assert updated["status"] == "approved"
    assert updated["started_at"] is None
    assert updated["error"] == "breaker_half_open_probing (cooldown 0s)"
    assert updated["metadata"]["next_attempt_at"]
    assert updated["metadata"]["dispatch_retry_after_s"] == 300
    assert tq.list_outcomes() == []


def test_task_queue_defers_timeout_dispatch_errors(monkeypatch, tmp_path):
    class _Gate:
        allowed = True
        requires_ack = False
        reason = ""

    class _Result:
        ok = False
        text = ""
        error = "timeout after 130s: Hermes gateway banner"

    fake_cli = type(sys)("cli_llm")
    fake_cli.dispatch = lambda **_kwargs: _Result()
    fake_autonomy = type(sys)("autonomy")
    fake_autonomy.authorize = lambda *_args, **_kwargs: _Gate()
    monkeypatch.setitem(sys.modules, "cli_llm", fake_cli)
    monkeypatch.setitem(sys.modules, "autonomy", fake_autonomy)

    tq = TaskQueue(tmp_path / "autonomy.db")
    task = tq.create_task(
        title="Handoff from jenna",
        description="Verify a brain knowledge gap.",
        assigned_agent="liz",
        confidence=0.9,
    )
    tq.approve_task(task["id"], by="test")

    tq.process_ready()
    updated = tq.get_task(task["id"])

    assert updated["status"] == "approved"
    assert updated["started_at"] is None
    assert "timeout after" in updated["error"]
    assert updated["metadata"]["dispatch_retry_after_s"] == 600
    assert tq.list_outcomes() == []


def test_task_queue_requeues_running_orphans(tmp_path):
    tq = TaskQueue(tmp_path / "autonomy.db")
    task = tq.create_task(
        title="Handoff from jenna",
        description="Verify a brain knowledge gap.",
        assigned_agent="ellie",
        confidence=0.9,
    )
    tq.approve_task(task["id"], by="test")
    tq.start_task(task["id"], by="test")

    assert tq.requeue_running_orphans(by="test_startup") == 1
    updated = tq.get_task(task["id"])

    assert updated["status"] == "approved"
    assert updated["started_at"] is None
    assert updated["metadata"]["orphaned_running_requeued_by"] == "test_startup"
    assert updated["execution_log"][-1]["reason"] == "running task orphaned by server restart"


def test_task_queue_notifies_only_human_blocker(monkeypatch, tmp_path):
    llm_calls: list[dict] = []
    telegram_calls: list[dict] = []

    fake_cli = type(sys)("cli_llm")
    fake_cli.dispatch = lambda **kwargs: llm_calls.append(kwargs)
    fake_telegram = type(sys)("telegram_alert")
    fake_telegram.send_chris_telegram = lambda *args, **kwargs: telegram_calls.append(kwargs) or True
    monkeypatch.setitem(sys.modules, "cli_llm", fake_cli)
    monkeypatch.setitem(sys.modules, "telegram_alert", fake_telegram)

    tq = TaskQueue(tmp_path / "autonomy.db")
    tq._escalate_tasks(
        [
            {
                "id": "task_human",
                "title": "Account login blocked",
                "description": "Need Chris 2FA code to continue.",
                "confidence": 0.2,
                "assigned_agent": "ellie",
                "metadata": {},
            }
        ]
    )

    assert llm_calls == []
    assert len(telegram_calls) == 1


def test_task_queue_llm_can_still_request_human_when_knowledge_missing(monkeypatch, tmp_path):
    telegram_calls: list[dict] = []

    class _Result:
        ok = True
        text = "HUMAN_NEEDED: Chris must provide the missing private preference."

    fake_cli = type(sys)("cli_llm")
    fake_cli.dispatch = lambda **_kwargs: _Result()
    fake_telegram = type(sys)("telegram_alert")
    fake_telegram.send_chris_telegram = (
        lambda body, **kwargs: telegram_calls.append({"body": body, **kwargs}) or True
    )
    monkeypatch.setitem(sys.modules, "cli_llm", fake_cli)
    monkeypatch.setitem(sys.modules, "telegram_alert", fake_telegram)

    tq = TaskQueue(tmp_path / "autonomy.db")
    tq._escalate_tasks(
        [
            {
                "id": "task_unclear",
                "title": "Choose preferred dashboard layout",
                "description": "Decide between compact and spacious layout.",
                "confidence": 0.4,
                "assigned_agent": "sage",
                "metadata": {},
            }
        ]
    )

    assert len(telegram_calls) == 1
    assert "HUMAN_NEEDED" in telegram_calls[0]["body"]


def test_task_queue_human_needed_notification_is_per_task(monkeypatch, tmp_path):
    responses = {
        "task_one": "HANDLEABLE: Assigned agent can inspect logs.",
        "task_two": "HUMAN_NEEDED: Chris must provide the missing private fact.",
    }
    telegram_calls: list[dict] = []

    def _dispatch(**kwargs):
        message = kwargs["message"]

        class _Result:
            ok = True
            text = responses["task_two" if "task_two" in message else "task_one"]

        return _Result()

    fake_cli = type(sys)("cli_llm")
    fake_cli.dispatch = _dispatch
    fake_telegram = type(sys)("telegram_alert")
    fake_telegram.send_chris_telegram = (
        lambda body, **kwargs: telegram_calls.append({"body": body, **kwargs}) or True
    )
    monkeypatch.setitem(sys.modules, "cli_llm", fake_cli)
    monkeypatch.setitem(sys.modules, "telegram_alert", fake_telegram)

    tq = TaskQueue(tmp_path / "autonomy.db")
    tq._review_tasks_with_subscription_llm(
        [
            {
                "id": "task_one",
                "title": "Investigate local log issue",
                "description": "Handle locally.",
                "confidence": 0.2,
                "assigned_agent": "sage",
                "metadata": {},
            },
            {
                "id": "task_two",
                "title": "Choose private preference",
                "description": "Needs a personal fact.",
                "confidence": 0.2,
                "assigned_agent": "sage",
                "metadata": {},
            },
        ]
    )

    assert len(telegram_calls) == 1
    assert "task_two" in telegram_calls[0]["body"]
    assert "task_one" not in telegram_calls[0]["body"]
