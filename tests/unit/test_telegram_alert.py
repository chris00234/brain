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

import telegram_alert  # noqa: E402


def _reset_state():
    telegram_alert._last_sent.clear()


def test_empty_body_returns_false():
    _reset_state()
    assert telegram_alert.send_chris_telegram("", source="test") is False
    assert telegram_alert.send_chris_telegram("   ", source="test") is False


def test_successful_send_stamps_rate_limit():
    _reset_state()

    class _OK:
        returncode = 0
        stdout = "sent"
        stderr = ""

    with (
        patch.object(telegram_alert, "OPENCLAW_BIN", "/bin/true"),
        patch("pathlib.Path.exists", return_value=True),
        patch("subprocess.run", return_value=_OK()),
    ):
        assert telegram_alert.send_chris_telegram("hi", source="test", severity="warn") is True
    # Second immediate send must be rate-limited
    with patch.object(telegram_alert, "OPENCLAW_BIN", "/bin/true"):
        assert telegram_alert.send_chris_telegram("hi again", source="test", severity="warn") is False


def test_failed_send_does_not_stamp_rate_limit():
    """Regression: previously a failed send burned the rate-limit window.
    Now the window is only consumed on confirmed delivery."""
    _reset_state()

    class _FAIL:
        returncode = 1
        stdout = ""
        stderr = "upstream down"

    class _OK:
        returncode = 0
        stdout = "sent"
        stderr = ""

    with (
        patch.object(telegram_alert, "OPENCLAW_BIN", "/bin/true"),
        patch("pathlib.Path.exists", return_value=True),
        patch("subprocess.run", return_value=_FAIL()),
    ):
        assert telegram_alert.send_chris_telegram("first", source="retry_test", severity="warn") is False

    # Next attempt should NOT be rate-limited because first one failed.
    with (
        patch.object(telegram_alert, "OPENCLAW_BIN", "/bin/true"),
        patch("pathlib.Path.exists", return_value=True),
        patch("subprocess.run", return_value=_OK()),
    ):
        assert telegram_alert.send_chris_telegram("retry", source="retry_test", severity="warn") is True


def test_failed_send_enqueues_backlog():
    _reset_state()
    calls: list = []

    class _FAIL:
        returncode = 17
        stdout = ""
        stderr = "nope"

    def _fake_enqueue(kind, payload):
        calls.append((kind, payload))

    with (
        patch.object(telegram_alert, "OPENCLAW_BIN", "/bin/true"),
        patch("pathlib.Path.exists", return_value=True),
        patch("subprocess.run", return_value=_FAIL()),
        patch.dict("sys.modules", {"llm_backlog": type("M", (), {"enqueue": staticmethod(_fake_enqueue)})()}),
    ):
        telegram_alert.send_chris_telegram("hi", source="backlog_test")

    assert len(calls) == 1
    assert calls[0][0] == "telegram"
    assert calls[0][1]["failure_reason"].startswith("rc=")


def test_bypass_rate_limit_always_sends():
    _reset_state()

    class _OK:
        returncode = 0
        stdout = ""
        stderr = ""

    telegram_alert._last_sent[("bypass_test", "warn")] = 9999999999.0  # very-recent fake stamp

    with (
        patch.object(telegram_alert, "OPENCLAW_BIN", "/bin/true"),
        patch("pathlib.Path.exists", return_value=True),
        patch("subprocess.run", return_value=_OK()),
    ):
        assert telegram_alert.send_chris_telegram(
            "probe", source="bypass_test", severity="warn", bypass_rate_limit=True
        ) is True


def test_missing_binary_queues_backlog():
    _reset_state()
    calls: list = []

    def _fake_enqueue(kind, payload):
        calls.append((kind, payload))

    with (
        patch.object(telegram_alert, "OPENCLAW_BIN", "/nonexistent/openclaw"),
        patch("pathlib.Path.exists", return_value=False),
        patch.dict("sys.modules", {"llm_backlog": type("M", (), {"enqueue": staticmethod(_fake_enqueue)})()}),
    ):
        assert telegram_alert.send_chris_telegram("hi", source="missing_test") is False

    assert len(calls) == 1
    assert calls[0][1]["failure_reason"] == "openclaw_missing"
