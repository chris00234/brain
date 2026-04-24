from __future__ import annotations

import json
import sys
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


def test_urgent_scan_writes_codex_doorbell(monkeypatch, tmp_path):
    monkeypatch.setattr(speak_urgent, "DOORBELL_DIR", tmp_path)
    (tmp_path / ".codex_turn_codex-session").write_text("1")
    emitted = []

    monkeypatch.setattr(speak_urgent, "ensure_schema", lambda: None)
    monkeypatch.setattr(
        speak_urgent,
        "collect_observations",
        lambda: [
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


def test_urgent_scan_uses_telegram_fallback_without_active_sessions(monkeypatch, tmp_path):
    monkeypatch.setattr(speak_urgent, "DOORBELL_DIR", tmp_path)
    emitted = []
    sent = []

    monkeypatch.setattr(speak_urgent, "ensure_schema", lambda: None)
    monkeypatch.setattr(
        speak_urgent,
        "collect_observations",
        lambda: [
            Observation(
                drive="synthesis_drive",
                category="pattern",
                severity=7.9,
                message="No active sessions should fall back to Telegram.",
                dedup_key="urgent-no-session",
            )
        ],
    )
    monkeypatch.setattr(speak_urgent, "was_sent_recently", lambda *args, **kwargs: False)
    monkeypatch.setattr(speak_urgent, "log_emit", lambda obs, sent_via: emitted.append((obs, sent_via)))

    class _Telegram:
        @staticmethod
        def send_chris_telegram(body, source, severity):
            sent.append((body, source, severity))
            return True

    monkeypatch.setitem(sys.modules, "telegram_alert", _Telegram)

    result = speak_urgent.urgent_scan()

    assert result == {
        "urgent": 1,
        "fired": 0,
        "active_sessions": 0,
        "fallback_via": "telegram:fallback",
    }
    assert sent[0][1:] == ("brain_speak_urgent:no_active_sessions", "urgent")
    assert emitted[0][1] == "telegram:fallback"
