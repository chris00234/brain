"""Unit tests for brain_core/telegram_alert.py — the unified direct
Bot-API helper that replaced 4 separate Telegram paths on 2026-04-17.

Goals:
  - Rate-limit stamp only happens on successful delivery (regression
    test for the bug that burned the window on failed sends).
  - Backlog fallback fires on every failure path.
  - bypass_rate_limit honored for probes/tests.
  - Empty body skipped.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core"))

import telegram_alert


def _reset_state():
    telegram_alert._last_sent.clear()


def test_empty_body_returns_false():
    _reset_state()
    assert telegram_alert.send_chris_telegram("", source="test") is False
    assert telegram_alert.send_chris_telegram("   ", source="test") is False


def test_successful_send_stamps_rate_limit():
    _reset_state()

    with (
        patch.object(telegram_alert, "_send_direct_bot_api", return_value=(True, "ok")),
    ):
        assert telegram_alert.send_chris_telegram("hi", source="test", severity="warn") is True
    # Second immediate send must be rate-limited
    assert telegram_alert.send_chris_telegram("hi again", source="test", severity="warn") is False


def test_failed_send_does_not_stamp_rate_limit():
    """Regression: previously a failed send burned the rate-limit window.
    Now the window is only consumed on confirmed delivery."""
    _reset_state()

    with (
        patch.object(telegram_alert, "_send_direct_bot_api", return_value=(False, "upstream_down")),
    ):
        assert (
            telegram_alert.send_chris_telegram(
                "first",
                source="retry_test",
                severity="warn",
                queue_on_failure=False,
            )
            is False
        )

    # Next attempt should NOT be rate-limited because first one failed.
    with (
        patch.object(telegram_alert, "_send_direct_bot_api", return_value=(True, "ok")),
    ):
        assert telegram_alert.send_chris_telegram("retry", source="retry_test", severity="warn") is True


def test_failed_send_enqueues_backlog():
    _reset_state()
    calls: list = []

    def _fake_enqueue(kind, payload):
        calls.append((kind, payload))

    with (
        patch.object(telegram_alert, "_send_direct_bot_api", return_value=(False, "http=500")),
        patch.dict("sys.modules", {"llm_backlog": type("M", (), {"enqueue": staticmethod(_fake_enqueue)})()}),
    ):
        telegram_alert.send_chris_telegram("hi", source="backlog_test")

    assert len(calls) == 1
    assert calls[0][0] == "telegram"
    assert calls[0][1]["failure_reason"] == "http=500"


def test_failed_send_can_skip_backlog_enqueue():
    _reset_state()
    calls: list = []

    def _fake_enqueue(kind, payload):
        calls.append((kind, payload))

    with (
        patch.object(telegram_alert, "_send_direct_bot_api", return_value=(False, "http=500")),
        patch.dict("sys.modules", {"llm_backlog": type("M", (), {"enqueue": staticmethod(_fake_enqueue)})()}),
    ):
        assert (
            telegram_alert.send_chris_telegram(
                "hi",
                source="backlog_replay",
                queue_on_failure=False,
            )
            is False
        )

    assert calls == []


def test_bypass_rate_limit_always_sends():
    _reset_state()

    telegram_alert._last_sent[("bypass_test", "warn")] = 9999999999.0  # very-recent fake stamp

    with (
        patch.object(telegram_alert, "_send_direct_bot_api", return_value=(True, "ok")),
    ):
        assert (
            telegram_alert.send_chris_telegram(
                "probe", source="bypass_test", severity="warn", bypass_rate_limit=True
            )
            is True
        )


def test_missing_token_queues_backlog():
    _reset_state()
    calls: list = []

    def _fake_enqueue(kind, payload):
        calls.append((kind, payload))

    with (
        patch.object(telegram_alert, "_send_direct_bot_api", return_value=(False, "telegram_token_missing")),
        patch.dict("sys.modules", {"llm_backlog": type("M", (), {"enqueue": staticmethod(_fake_enqueue)})()}),
    ):
        assert telegram_alert.send_chris_telegram("hi", source="missing_test") is False

    assert len(calls) == 1
    assert calls[0][1]["failure_reason"] == "telegram_token_missing"


def test_reads_jenna_token_from_hermes_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text('export TELEGRAM_JENNA_TOKEN="123:abc"\n')
    monkeypatch.delenv("TELEGRAM_JENNA_TOKEN", raising=False)
    monkeypatch.setattr(telegram_alert, "HERMES_ENV_FILE", env_file)

    assert telegram_alert._telegram_token() == "123:abc"


def test_direct_api_healthcheck_uses_get_chat(monkeypatch):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    calls = []

    def fake_urlopen(req, timeout):
        calls.append((req, timeout))
        return Response()

    monkeypatch.setattr(telegram_alert, "_telegram_token", lambda: "123:abc")
    monkeypatch.setattr(telegram_alert.urllib.request, "urlopen", fake_urlopen)

    ok, reason = telegram_alert.direct_api_healthcheck()

    assert ok is True
    assert reason == "ok"
    assert calls[0][0].full_url.endswith("/getChat")
