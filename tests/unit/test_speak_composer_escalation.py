from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core"))

import speak_composer
from speak_schema import Observation


def test_digest_observation_self_handled_by_subscription_agent(monkeypatch):
    calls = []

    fake_agent_messenger = type(sys)("agent_messenger")
    fake_agent_messenger.send_message = lambda **kwargs: calls.append(kwargs) or "msg-1"
    monkeypatch.setitem(sys.modules, "agent_messenger", fake_agent_messenger)

    fake_cli = type(sys)("cli_llm")
    fake_cli.dispatch = lambda **_kwargs: (_ for _ in ()).throw(AssertionError("CLI fallback not expected"))
    monkeypatch.setitem(sys.modules, "cli_llm", fake_cli)

    notify, self_handled = speak_composer.route_digest_observations(
        [
            Observation(
                drive="contradiction_drive",
                category="contradiction",
                severity=7.5,
                message="Potential contradiction agents can investigate.",
                dedup_key="c1",
            )
        ]
    )

    assert notify == []
    assert self_handled == 1
    assert len(calls) == 1
    assert calls[0]["to_agent"] == "sage"


def test_digest_observation_notifies_when_llm_requests_human(monkeypatch):
    class _Result:
        ok = True
        text = "HUMAN_NEEDED: Chris must provide the missing private fact."

    fake_cli = type(sys)("cli_llm")
    fake_cli.dispatch = lambda **_kwargs: _Result()
    fake_agent_messenger = type(sys)("agent_messenger")
    fake_agent_messenger.send_message = lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("handoff down"))
    monkeypatch.setitem(sys.modules, "agent_messenger", fake_agent_messenger)
    monkeypatch.setitem(sys.modules, "cli_llm", fake_cli)

    notify, self_handled = speak_composer.route_digest_observations(
        [
            Observation(
                drive="contradiction_drive",
                category="contradiction",
                severity=7.5,
                message="This may depend on a private preference.",
                dedup_key="c2",
            )
        ]
    )

    assert self_handled == 0
    assert len(notify) == 1
    assert notify[0].category == "human-needed"
