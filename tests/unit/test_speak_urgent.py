from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core"))

import speak_urgent
from speak_schema import Observation


def test_active_session_ids_include_codex_and_claude_turn_files(monkeypatch, tmp_path):
    monkeypatch.setattr(speak_urgent, "DOORBELL_DIR", tmp_path)
    (tmp_path / ".claude_turn_claude-session").write_text("1")
    (tmp_path / ".codex_turn_codex-session").write_text("1")
    (tmp_path / ".codex_turn_anon").write_text("1")

    assert speak_urgent._active_session_ids() == ["claude-session", "codex-session"]


def test_active_session_ids_ignore_stale_and_cap_newest(monkeypatch, tmp_path):
    monkeypatch.setattr(speak_urgent, "DOORBELL_DIR", tmp_path)
    monkeypatch.setattr(speak_urgent, "ACTIVE_SESSION_WINDOW_S", 600)
    monkeypatch.setattr(speak_urgent, "MAX_ACTIVE_SESSIONS", 2)
    now = time.time()
    for idx in range(4):
        f = tmp_path / f".codex_turn_session-{idx}"
        f.write_text("1")
        os.utime(f, (now - idx, now - idx))
    stale = tmp_path / ".codex_turn_stale"
    stale.write_text("1")
    os.utime(stale, (now - 3600, now - 3600))

    assert speak_urgent._active_session_ids() == ["session-0", "session-1"]


def test_cleanup_stale_doorbells_removes_old_files(monkeypatch, tmp_path):
    monkeypatch.setattr(speak_urgent, "DOORBELL_DIR", tmp_path)
    now = time.time()
    old = tmp_path / ".brain_doorbell.old.jsonl"
    fresh = tmp_path / ".brain_doorbell.fresh.jsonl"
    old.write_text("{}\n")
    fresh.write_text("{}\n")
    os.utime(old, (now - 1000, now - 1000))

    removed = speak_urgent._cleanup_stale_doorbells(max_age_s=120)

    assert removed == 1
    assert not old.exists()
    assert fresh.exists()


def test_urgent_scan_writes_codex_doorbell(monkeypatch, tmp_path):
    monkeypatch.setattr(speak_urgent, "DOORBELL_DIR", tmp_path)
    (tmp_path / ".codex_turn_codex-session").write_text("1")
    emitted = []

    monkeypatch.setattr(speak_urgent, "ensure_schema", lambda: None)
    monkeypatch.setattr(
        speak_urgent,
        "collect_observations",
        lambda **_kwargs: [
            Observation(
                drive="synthesis_drive",
                category="pattern",
                severity=7.8,
                message="Codex urgent path should receive this.",
                dedup_key="urgent-codex",
            )
        ],
    )
    monkeypatch.setattr(speak_urgent, "was_sent_recently", lambda *args, **kwargs: False)
    monkeypatch.setattr(speak_urgent, "log_emit", lambda obs, sent_via: emitted.append((obs, sent_via)))

    result = speak_urgent.urgent_scan()

    doorbell = tmp_path / ".brain_doorbell.codex-session.jsonl"
    assert result == {"urgent": 1, "fired": 1, "active_sessions": 1, "fallback_via": None}
    assert doorbell.exists()
    row = json.loads(doorbell.read_text().strip())
    assert row["source"] == "brain_speak_urgent"
    assert row["content"] == "Codex urgent path should receive this."
    assert emitted[0][1] == "doorbell:1sessions"


def test_urgent_scan_uses_agent_fallback_without_active_sessions(monkeypatch, tmp_path):
    monkeypatch.setattr(speak_urgent, "DOORBELL_DIR", tmp_path)
    emitted = []
    agent_calls = []

    monkeypatch.setattr(speak_urgent, "ensure_schema", lambda: None)
    monkeypatch.setattr(
        speak_urgent,
        "collect_observations",
        lambda **_kwargs: [
            Observation(
                drive="synthesis_drive",
                category="pattern",
                severity=7.9,
                message="No active sessions should route to subscription LLM.",
                dedup_key="urgent-no-session",
            )
        ],
    )
    monkeypatch.setattr(speak_urgent, "was_sent_recently", lambda *args, **kwargs: False)
    monkeypatch.setattr(speak_urgent, "log_emit", lambda obs, sent_via: emitted.append((obs, sent_via)))

    fake_agent_messenger = type(sys)("agent_messenger")
    fake_agent_messenger.send_message = lambda **kwargs: agent_calls.append(kwargs) or "msg-1"
    monkeypatch.setitem(sys.modules, "agent_messenger", fake_agent_messenger)

    fake_cli = type(sys)("cli_llm")
    fake_cli.dispatch = lambda **_kwargs: (_ for _ in ()).throw(AssertionError("CLI fallback not expected"))
    monkeypatch.setitem(sys.modules, "cli_llm", fake_cli)

    result = speak_urgent.urgent_scan()

    assert result == {
        "urgent": 1,
        "fired": 0,
        "active_sessions": 0,
        "fallback_via": "agent:self_handled",
    }
    assert len(agent_calls) == 1
    assert agent_calls[0]["to_agent"] == "sage"
    assert emitted[0][1] == "agent:self_handled"


def test_urgent_scan_telegram_only_when_llm_requests_human(monkeypatch, tmp_path):
    monkeypatch.setattr(speak_urgent, "DOORBELL_DIR", tmp_path)
    emitted = []
    sent = []

    monkeypatch.setattr(speak_urgent, "ensure_schema", lambda: None)
    monkeypatch.setattr(
        speak_urgent,
        "collect_observations",
        lambda **_kwargs: [
            Observation(
                drive="synthesis_drive",
                category="pattern",
                severity=8.2,
                message="Layout preference requires private knowledge.",
                dedup_key="urgent-human-needed",
            )
        ],
    )
    monkeypatch.setattr(speak_urgent, "was_sent_recently", lambda *args, **kwargs: False)
    monkeypatch.setattr(speak_urgent, "log_emit", lambda obs, sent_via: emitted.append((obs, sent_via)))

    class _Result:
        ok = True
        text = "HUMAN_NEEDED: Chris must provide the missing private preference."

    fake_cli = type(sys)("cli_llm")
    fake_cli.dispatch = lambda **_kwargs: _Result()
    fake_agent_messenger = type(sys)("agent_messenger")
    fake_agent_messenger.send_message = lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("handoff down"))

    class _Telegram:
        @staticmethod
        def send_chris_telegram(body, source, severity):
            sent.append((body, source, severity))
            return True

    monkeypatch.setitem(sys.modules, "agent_messenger", fake_agent_messenger)
    monkeypatch.setitem(sys.modules, "cli_llm", fake_cli)
    monkeypatch.setitem(sys.modules, "telegram_alert", _Telegram)

    result = speak_urgent.urgent_scan()

    assert result["fallback_via"] == "telegram:fallback"
    assert sent[0][1:] == ("brain_speak_urgent:human_required", "urgent")
    assert emitted[0][1] == "telegram:fallback"
