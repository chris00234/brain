"""Unified Telegram alert delivery.

All Chris-facing alerts — SLO breaches, job failures, brain_loop URGENT
messages, healthcheck alerts, LoRA regressions — go through this single
entry point. Bypasses any LLM session: uses `openclaw message send
--channel telegram`, which is a direct Bot API POST at the gateway layer.

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
  consolidated into this module. LLM-free, subprocess-only, with
  llm_backlog auto-fallback.

Contract:
- `send_chris_telegram(body, source, severity="warn")` → bool
- Returns True if delivered, False if queued to backlog or skipped.
- Never raises; never blocks the caller longer than 20s.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

OPENCLAW_BIN = "/Users/chrischo/.local/bin/openclaw"
CHRIS_TELEGRAM_CHAT_ID = "8484060831"
TELEGRAM_ACCOUNT = "jenna-bot"
SEND_TIMEOUT_S = 20

# ── Rate limiter ────────────────────────────────────────────
# Prevent alert storms. Each (source, severity) bucket can emit once per
# window. Critical alerts have shorter windows than warnings.
_RATE_LIMITS = {
    "critical": 300.0,  # 5 min
    "urgent": 600.0,    # 10 min (brain_loop.URGENT)
    "warn": 1800.0,     # 30 min
    "info": 7200.0,     # 2 hours
}
_last_sent: dict[tuple[str, str], float] = {}
_lock = threading.Lock()


def _rate_limited(source: str, severity: str) -> bool:
    """True if (source, severity) was sent recently — caller should skip."""
    window = _RATE_LIMITS.get(severity, 1800.0)
    key = (source, severity)
    now = time.time()
    with _lock:
        last = _last_sent.get(key, 0.0)
        if now - last < window:
            return True
        _last_sent[key] = now
        return False


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


def send_chris_telegram(
    body: str,
    source: str,
    severity: str = "warn",
    *,
    bypass_rate_limit: bool = False,
) -> bool:
    """Send a Telegram alert to Chris.

    Args:
        body: message text (will be truncated to 4000 chars)
        source: identifier for rate-limiting (e.g. "slo:breaker_open_count",
                "brain_loop", "scheduler:job_failure")
        severity: one of "critical", "urgent", "warn", "info" — controls
                  rate-limit window
        bypass_rate_limit: set True for probe/test sends

    Returns True on delivery, False on rate-limit / send failure
    (failures are auto-queued to llm_backlog kind=telegram).
    """
    if not bypass_rate_limit and _rate_limited(source, severity):
        log.debug("telegram rate-limited: source=%s severity=%s", source, severity)
        return False

    body = (body or "")[:4000]
    if not body.strip():
        return False

    if not Path(OPENCLAW_BIN).exists():
        log.warning("openclaw binary missing — queuing telegram alert for %s", source)
        _queue_backlog(body, source, severity, "openclaw_missing")
        return False

    try:
        proc = subprocess.run(
            [
                OPENCLAW_BIN,
                "message",
                "send",
                "--channel",
                "telegram",
                "--target",
                CHRIS_TELEGRAM_CHAT_ID,
                "--account",
                TELEGRAM_ACCOUNT,
                "--message",
                body,
            ],
            capture_output=True,
            text=True,
            timeout=SEND_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        log.warning("telegram send timeout (%ds) source=%s", SEND_TIMEOUT_S, source)
        _queue_backlog(body, source, severity, "timeout")
        return False
    except Exception as exc:
        log.warning("telegram send failed source=%s: %s", source, exc)
        _queue_backlog(body, source, severity, f"exc:{type(exc).__name__}")
        return False

    if proc.returncode != 0:
        log.warning(
            "telegram send rc=%d source=%s stderr=%s",
            proc.returncode,
            source,
            (proc.stderr or "")[:200],
        )
        _queue_backlog(body, source, severity, f"rc={proc.returncode}")
        return False

    return True
