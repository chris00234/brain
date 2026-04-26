"""brain_core/speak_urgent.py — brain's real-time interrupt path.

Writes severity >= URGENT_SEVERITY_THRESHOLD observations to active
Claude Code/Codex session doorbells so boot hooks pick them up next turn.

Split from speak.py 2026-04-23.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from speak_composer import collect_observations
from speak_schema import Observation, ensure_schema, log_emit, now_iso, was_sent_recently

log = logging.getLogger("brain.speak")

URGENT_SEVERITY_THRESHOLD = 7.5
DOORBELL_DIR = Path("/tmp")
ACTIVE_SESSION_WINDOW_S = int(os.getenv("BRAIN_DOORBELL_ACTIVE_WINDOW_S", "600"))
MAX_ACTIVE_SESSIONS = int(os.getenv("BRAIN_DOORBELL_MAX_ACTIVE_SESSIONS", "3"))
STALE_DOORBELL_MAX_AGE_S = int(os.getenv("BRAIN_DOORBELL_STALE_AGE_S", "900"))


def _active_session_ids() -> list[str]:
    """Discover active Claude Code/Codex sessions from turn marker files.

    claude_boot.sh and codex_boot.sh write one file per session on each turn.
    Any file modified in the recent active window is considered active, capped
    to the newest few sessions. Codex can create many short-lived session ids;
    without this cap a single urgent scan fans out to dozens of stale sessions.
    """
    now = datetime.now(UTC).timestamp()
    candidates: list[tuple[float, str]] = []
    try:
        for prefix in (".claude_turn_", ".codex_turn_"):
            for f in DOORBELL_DIR.glob(f"{prefix}*"):
                try:
                    mtime = f.stat().st_mtime
                    if (now - mtime) > max(60, ACTIVE_SESSION_WINDOW_S):
                        continue
                except OSError:
                    continue
                sid = f.name[len(prefix) :]
                if sid and sid != "anon":
                    candidates.append((mtime, sid))
    except Exception as exc:
        log.debug("active session scan failed: %s", exc)
    active: list[str] = []
    seen: set[str] = set()
    for _mtime, sid in sorted(candidates, reverse=True):
        if sid in seen:
            continue
        seen.add(sid)
        active.append(sid)
        if len(active) >= max(1, MAX_ACTIVE_SESSIONS):
            break
    return sorted(active)


def _cleanup_stale_doorbells(max_age_s: int = STALE_DOORBELL_MAX_AGE_S) -> int:
    """Remove doorbell files for sessions that are no longer active."""
    now = datetime.now(UTC).timestamp()
    removed = 0
    try:
        for f in DOORBELL_DIR.glob(".brain_doorbell.*.jsonl"):
            try:
                if (now - f.stat().st_mtime) <= max(60, max_age_s):
                    continue
                f.unlink()
                removed += 1
            except OSError:
                continue
    except Exception as exc:
        log.debug("stale doorbell cleanup failed: %s", exc)
    return removed


def urgent_scan() -> dict:
    """Fire path for observations with severity >= URGENT_SEVERITY_THRESHOLD.

    Runs drives, picks observations above the urgent bar not sent in the
    last 6h, and writes them to /tmp/.brain_doorbell.<sid>.jsonl for every
    active Claude Code/Codex session. Boot hooks read + consume + delete
    those files on the next turn so the agent sees the message in its
    system-reminder block.

    Runs every 5 min via cron. Independent of the 07:55 morning digest.
    """
    ensure_schema()
    _cleanup_stale_doorbells()
    obs = collect_observations()
    urgent = [o for o in obs if o.severity >= URGENT_SEVERITY_THRESHOLD]
    urgent_fresh = [o for o in urgent if not was_sent_recently(f"doorbell:{o.dedup_key}", within_h=6)]
    if not urgent_fresh:
        return {"urgent": 0, "fired": 0, "active_sessions": 0}

    active = _active_session_ids()
    fired = 0
    fallback_via: str | None = None
    if active:
        for sid in active:
            doorbell_path = DOORBELL_DIR / f".brain_doorbell.{sid}.jsonl"
            try:
                with doorbell_path.open("a") as f:
                    for o in urgent_fresh:
                        f.write(
                            json.dumps(
                                {
                                    "source": "brain_speak_urgent",
                                    "priority": "high" if o.severity >= 8 else "medium",
                                    "title": f"{o.drive} / {o.category}",
                                    "content": o.message,
                                    "severity": o.severity,
                                    "ts": now_iso(),
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                        fired += 1
            except OSError as exc:
                log.warning("doorbell write failed for %s: %s", sid, exc)
                continue
    else:
        fallback_via = _telegram_fallback(urgent_fresh)

    for o in urgent_fresh:
        try:
            sent_via = f"doorbell:{len(active)}sessions" if active else fallback_via
            log_emit(
                Observation(
                    drive=o.drive,
                    category=o.category,
                    severity=o.severity,
                    message=o.message,
                    dedup_key=f"doorbell:{o.dedup_key}",
                    payload={**o.payload, "fired_to_sessions": len(active), "fallback_via": fallback_via},
                ),
                sent_via=sent_via,
            )
        except Exception as exc:
            log.debug("urgent log failed: %s", exc)

    return {
        "urgent": len(urgent_fresh),
        "fired": fired,
        "active_sessions": len(active),
        "fallback_via": fallback_via,
    }


def _telegram_fallback(observations: list[Observation]) -> str:
    """Fallback for moments with no active CLI session to receive doorbells."""
    if not observations:
        return "skipped:no_observations"
    try:
        from telegram_alert import send_chris_telegram
    except Exception as exc:
        log.warning("telegram fallback unavailable: %s", exc)
        return "queued:telegram_unavailable"

    lines = ["[brain_speak_urgent] no active CLI sessions; urgent observations:"]
    for o in observations[:5]:
        lines.append(f"- [{o.severity:.1f}] {o.drive}/{o.category}: {o.message[:500]}")
    delivered = send_chris_telegram(
        "\n".join(lines),
        source="brain_speak_urgent:no_active_sessions",
        severity="urgent",
    )
    return "telegram:fallback" if delivered else "queued:telegram_fallback"
