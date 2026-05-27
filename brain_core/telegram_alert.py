"""Unified Telegram alert delivery.

All Chris-facing alerts — SLO breaches, job failures, brain_loop URGENT
messages, healthcheck alerts, LoRA regressions — go through this single
entry point. Bypasses any LLM session and bypasses Hermes profile gateways:
uses Telegram Bot API ``sendMessage`` directly.

Why this exists:
- 2026-04-17: brain_loop._telegram_alert was routing through
  `openclaw_dispatch(agent="jenna")`, which reuses the persistent
  `agent:jenna:main` codex session. That session accumulated 103MB (754K
  tokens) over 4 days and returned empty payloads 42.5% of the time,
  tripping the llm.dispatch breaker and losing alerts.
- Root cause: LLM-path for non-LLM work (alert text generation was not
  needed; the format is fixed).
- Fix: four separate telegram sender implementations (slos.py,
  brain_loop, scheduler._alert_failure, healthcheck, lora_ab_gate)
  consolidated into this module. LLM-free, OpenClaw-free, with
  llm_backlog auto-fallback.

Contract:
- `send_chris_telegram(body, source, severity="warn")` → bool
- Returns True if delivered, False if queued to backlog or skipped.
- Never raises; never blocks the caller longer than 20s.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

CHRIS_TELEGRAM_CHAT_ID = "8484060831"
TELEGRAM_TOKEN_ENVS = ("TELEGRAM_JENNA_TOKEN", "TELEGRAM_BOT_TOKEN")
HERMES_ENV_FILES = (
    Path("/Users/chrischo/.hermes/.env"),
    Path("/Users/chrischo/.hermes/profiles/jenna/.env"),
)
# Backward-compatible test/config hooks.
TELEGRAM_TOKEN_ENV = TELEGRAM_TOKEN_ENVS[0]
HERMES_ENV_FILE = HERMES_ENV_FILES[0]
SEND_TIMEOUT_S = 20

# ── Rate limiter ────────────────────────────────────────────
# Prevent alert storms. Each (source, severity) bucket can emit once per
# window. Critical alerts have shorter windows than warnings.
_RATE_LIMITS = {
    "critical": 300.0,  # 5 min
    "urgent": 600.0,  # 10 min (brain_loop.URGENT)
    "warn": 1800.0,  # 30 min
    "info": 7200.0,  # 2 hours
}
_last_sent: dict[tuple[str, str], float] = {}
_lock = threading.Lock()


def _should_rate_limit(source: str, severity: str) -> bool:
    """Check whether a send would be rate-limited. Does NOT stamp.

    2026-04-17 fix: previously, checking the limit also burned it. If
    the subprocess send then failed, the window was already consumed
    and the next legitimate alert (within 5-30 min depending on
    severity) would be silently rate-limited despite the prior
    never-delivered. Now the stamp only happens on confirmed delivery
    via `_mark_sent`."""
    window = _RATE_LIMITS.get(severity, 1800.0)
    key = (source, severity)
    now = time.time()
    with _lock:
        last = _last_sent.get(key, 0.0)
        return (now - last) < window


def _mark_sent(source: str, severity: str) -> None:
    """Record successful delivery timestamp. Call only after delivery."""
    key = (source, severity)
    with _lock:
        _last_sent[key] = time.time()


def _queue_backlog(body: str, source: str, severity: str, reason: str) -> None:
    """Enqueue for later retry when send fails."""
    try:
        from llm_backlog import enqueue as _enqueue

        _enqueue(
            "telegram",
            {
                "body": body,
                "source": source,
                "severity": severity,
                "failure_reason": reason,
            },
        )
    except Exception as exc:
        log.warning("backlog enqueue failed for %s: %s", source, exc)


def _read_shell_export(path: Path, name: str) -> str | None:
    try:
        for line in path.read_text(errors="ignore").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("export "):
                stripped = stripped[len("export ") :].strip()
            prefix = f"{name}="
            if not stripped.startswith(prefix):
                continue
            value = stripped[len(prefix) :].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            return value or None
    except OSError:
        return None
    return None


def _telegram_token() -> str | None:
    for name in TELEGRAM_TOKEN_ENVS:
        token = os.getenv(name)
        if token:
            return token

    files = tuple(dict.fromkeys((HERMES_ENV_FILE, *HERMES_ENV_FILES)))
    for path in files:
        for name in TELEGRAM_TOKEN_ENVS:
            token = _read_shell_export(path, name)
            if token:
                return token
    return None


def _send_direct_bot_api(body: str) -> tuple[bool, str]:
    token = _telegram_token()
    if not token:
        return False, "telegram_token_missing"
    data = urllib.parse.urlencode(
        {
            "chat_id": CHRIS_TELEGRAM_CHAT_ID,
            "text": body,
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=SEND_TIMEOUT_S) as resp:  # noqa: S310 - fixed Telegram API URL.
            status = getattr(resp, "status", 200)
            if 200 <= int(status) < 300:
                return True, "ok"
            return False, f"http={status}"
    except Exception as exc:
        return False, f"exc:{type(exc).__name__}"


def direct_api_healthcheck() -> tuple[bool, str]:
    """Verify token + Chris chat reachability without sending a message."""

    token = _telegram_token()
    if not token:
        return False, "telegram_token_missing"
    data = urllib.parse.urlencode({"chat_id": CHRIS_TELEGRAM_CHAT_ID}).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/getChat",
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=SEND_TIMEOUT_S) as resp:  # noqa: S310 - fixed Telegram API URL.
            status = getattr(resp, "status", 200)
            if 200 <= int(status) < 300:
                return True, "ok"
            return False, f"http={status}"
    except Exception as exc:
        return False, f"exc:{type(exc).__name__}"


def send_chris_telegram(
    body: str,
    source: str,
    severity: str = "warn",
    *,
    bypass_rate_limit: bool = False,
    queue_on_failure: bool = True,
) -> bool:
    """Send a Telegram alert to Chris.

    Args:
        body: message text (will be truncated to 4000 chars)
        source: identifier for rate-limiting (e.g. "slo:breaker_open_count",
                "brain_loop", "scheduler:job_failure")
        severity: one of "critical", "urgent", "warn", "info" — controls
                  rate-limit window
        bypass_rate_limit: set True for probe/test sends
        queue_on_failure: set False when replaying an existing telegram
                  backlog row so a failed replay does not enqueue a duplicate

    Returns True on delivery, False on rate-limit / send failure
    (failures are auto-queued to llm_backlog kind=telegram).
    """
    if not bypass_rate_limit and _should_rate_limit(source, severity):
        log.debug("telegram rate-limited: source=%s severity=%s", source, severity)
        return False

    body = (body or "")[:4000]
    if not body.strip():
        return False

    ok, reason = _send_direct_bot_api(body)
    if not ok:
        log.warning("telegram direct send failed source=%s reason=%s", source, reason)
        if queue_on_failure:
            _queue_backlog(body, source, severity, reason)
        return False

    # Only stamp the rate-limit clock on confirmed delivery
    _mark_sent(source, severity)
    return True
