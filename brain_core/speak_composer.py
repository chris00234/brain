"""brain_core/speak_composer.py — digest composition + delivery.

Orchestrates the drive fanout, ranks observations, formats the Telegram
body, delivers, and logs the outcome for ack tracking.

Split from speak.py 2026-04-23.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from speak_drives import coding_revert_drive, contradiction_drive, stale_thread_drive
from speak_schema import (
    DIGEST_MAX_BULLETS,
    Observation,
    brain_conn,
    ensure_schema,
    log_emit,
    now_iso,
    was_sent_recently,
)
from speak_synthesis import synthesis_drive

log = logging.getLogger("brain.speak")


DRIVES: list[Callable[[], list[Observation]]] = [
    contradiction_drive,
    coding_revert_drive,
    stale_thread_drive,
    synthesis_drive,
]


def collect_observations() -> list[Observation]:
    """Fan out all drives; swallow drive-level exceptions to keep loop robust."""
    ensure_schema()
    out: list[Observation] = []
    for drive in DRIVES:
        try:
            results = drive() or []
            out.extend(results)
        except Exception as exc:
            log.warning("drive %s raised: %s", drive.__name__, exc)
    return out


def compose_digest(
    observations: list[Observation], *, max_bullets: int = DIGEST_MAX_BULLETS
) -> list[Observation]:
    """Dedupe + rank. Returns the observations that should actually be sent."""
    fresh = [o for o in observations if not was_sent_recently(o.dedup_key)]
    fresh.sort(key=lambda o: (-o.severity, o.drive))
    return fresh[:max_bullets]


def format_telegram(chosen: list[Observation]) -> str:
    if not chosen:
        return ""
    lines = ["🧠 Brain digest — noticed overnight:"]
    for o in chosen:
        prefix = "⚠" if o.severity >= 7 else "•"
        lines.append(f"{prefix} [{o.category}] {o.message}")
    lines.append("")
    lines.append("ack: useful / noise / ignore")
    return "\n".join(lines)


def run_digest(*, dry_run: bool = False, bypass_dedup: bool = False) -> dict:
    """Main entry point — called from cron and from POST /brain/speak/run."""
    ensure_schema()
    all_obs = collect_observations()
    if bypass_dedup:
        chosen = sorted(all_obs, key=lambda o: -o.severity)[:DIGEST_MAX_BULLETS]
    else:
        chosen = compose_digest(all_obs)

    body = format_telegram(chosen)
    delivered = False
    if chosen and body and not dry_run:
        try:
            from telegram_alert import send_chris_telegram

            delivered = send_chris_telegram(
                body,
                source="brain_speak:digest",
                severity="info",
            )
        except Exception as exc:
            log.warning("telegram send failed: %s", exc)
            delivered = False

    logged_ids: list[str] = []
    if not dry_run:
        for o in chosen:
            try:
                logged_ids.append(log_emit(o, sent_via="telegram" if delivered else "queued"))
            except Exception as exc:
                log.debug("speak log write failed: %s", exc)

    return {
        "total_observations": len(all_obs),
        "chosen": [
            {
                "drive": o.drive,
                "category": o.category,
                "severity": o.severity,
                "message": o.message,
            }
            for o in chosen
        ],
        "delivered": delivered,
        "dry_run": dry_run,
        "body": body,
        "logged_ids": logged_ids,
    }


def recent_history(limit: int = 20) -> list[dict]:
    ensure_schema()
    with brain_conn() as conn:
        rows = conn.execute(
            "SELECT id, ts, drive, category, severity, message, sent_via, ack "
            "FROM brain_speak_log ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def ack(entry_id: str, verdict: str) -> bool:
    """Chris (or Jenna relay) marks a digest entry as useful / noise."""
    if verdict not in ("useful", "noise", "ignore"):
        return False
    with brain_conn() as conn:
        cur = conn.execute(
            "UPDATE brain_speak_log SET ack = ?, ack_ts = ? WHERE id = ?",
            (verdict, now_iso(), entry_id),
        )
        conn.commit()
        return cur.rowcount > 0
