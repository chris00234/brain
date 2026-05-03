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


RULE_BASED_DRIVES: list[Callable[[], list[Observation]]] = [
    contradiction_drive,
    coding_revert_drive,
    stale_thread_drive,
]
DRIVES: list[Callable[[], list[Observation]]] = [*RULE_BASED_DRIVES, synthesis_drive]


def collect_observations(*, include_synthesis: bool = True) -> list[Observation]:
    """Fan out all drives; swallow drive-level exceptions to keep loop robust."""
    ensure_schema()
    out: list[Observation] = []
    drives = DRIVES if include_synthesis else RULE_BASED_DRIVES
    for drive in drives:
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


def _observation_body(o: Observation) -> str:
    return f"[{o.drive}/{o.category}] severity={o.severity}: {o.message}"


def _self_handle_observation(o: Observation) -> Observation | None:
    """Route a digest observation to subscription LLM first.

    Returns a synthetic human-needed observation only when the LLM explicitly
    says Chris is required. Otherwise the observation is considered handled and
    omitted from Telegram.
    """
    body = _observation_body(o)
    try:
        from escalation_policy import classify_escalation, llm_review_prompt, llm_says_human_needed

        route = classify_escalation(title=f"{o.drive}:{o.category}", content=o.message, metadata=o.payload)
        if route.notify_human:
            return o

        try:
            from agent_messenger import send_message

            send_message(
                from_agent="brain_speak_digest",
                to_agent="sage",
                content=(
                    "Brain digest observation. Handle it yourself if possible; "
                    "notify Chris only for a true human blocker.\n\n" + body
                ),
                message_type="handoff",
                priority=4,
                metadata={"source": "brain_speak_digest", "dedup_key": o.dedup_key},
            )
            return None
        except Exception as exc:
            log.debug("digest agent handoff failed, falling back to CLI review: %s", exc)

        from cli_llm import dispatch

        result = dispatch(
            agent="sage",
            message=llm_review_prompt("brain_speak_digest", body),
            thinking="low",
            timeout=45,
            backlog_kind="proactive",
            backlog_payload={"source": "brain_speak_digest", "body": body},
        )
        if result.ok and llm_says_human_needed(result.text):
            return Observation(
                drive=o.drive,
                category="human-needed",
                severity=max(o.severity, 8.0),
                message=result.text[:800],
                dedup_key=f"human_needed:{o.dedup_key}",
                payload={**o.payload, "source_observation": o.dedup_key},
            )
        return None
    except Exception as exc:
        log.warning("digest self-handle failed for %s: %s", o.dedup_key, exc)
        return None


def route_digest_observations(chosen: list[Observation]) -> tuple[list[Observation], int]:
    """Split digest observations into Chris-needed vs self-handled."""
    notify: list[Observation] = []
    self_handled = 0
    for o in chosen:
        routed = _self_handle_observation(o)
        if routed is None:
            self_handled += 1
            continue
        notify.append(routed)
    return notify, self_handled


def run_digest(*, dry_run: bool = False, bypass_dedup: bool = False) -> dict:
    """Main entry point — called from cron and from POST /brain/speak/run."""
    ensure_schema()
    all_obs = collect_observations()
    if bypass_dedup:
        chosen = sorted(all_obs, key=lambda o: -o.severity)[:DIGEST_MAX_BULLETS]
    else:
        chosen = compose_digest(all_obs)

    if dry_run:
        notify_chosen = chosen
        self_handled = 0
    else:
        notify_chosen, self_handled = route_digest_observations(chosen)
    body = format_telegram(notify_chosen)
    delivered = False
    if notify_chosen and body and not dry_run:
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
        for o in notify_chosen:
            try:
                logged_ids.append(log_emit(o, sent_via="telegram" if delivered else "queued"))
            except Exception as exc:
                log.debug("speak log write failed: %s", exc)
        for o in chosen:
            if o in notify_chosen:
                continue
            try:
                logged_ids.append(log_emit(o, sent_via="self_handled"))
            except Exception as exc:
                log.debug("speak self-handled log write failed: %s", exc)

    return {
        "total_observations": len(all_obs),
        "chosen": [
            {
                "drive": o.drive,
                "category": o.category,
                "severity": o.severity,
                "message": o.message,
            }
            for o in notify_chosen
        ],
        "self_handled": self_handled,
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
