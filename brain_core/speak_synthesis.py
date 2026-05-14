"""brain_core/speak_synthesis.py — the thinking drive.

Uses cli_llm (Codex primary, Spark fallback) to read recent brain state and
surface PATTERNS the rule-based drives miss. Also proposes commands that can
be auto-dispatched when BRAIN_SYNTHESIS_AUTO_DISPATCH=1.

Split from speak.py 2026-04-23.
"""

from __future__ import annotations

import hashlib
import json as _json
import logging
import re as _re
import sqlite3

from speak_drives import _synthesis_auto_dispatch_enabled
from speak_schema import Observation, autonomy_conn, brain_conn, was_sent_recently

log = logging.getLogger("brain.speak")


def _gather_signals() -> tuple[list[dict], list[dict], list[dict]]:
    """Read contradictions (24h), coding_events (24h), focus/session (7d)."""
    try:
        with brain_conn() as conn:
            contradictions = [
                dict(r)
                for r in conn.execute(
                    "SELECT summary, detail, created_at FROM attention_queue "
                    "WHERE dismissed=0 AND category='contradiction' "
                    "  AND created_at >= datetime('now', '-1 day') "
                    "ORDER BY created_at DESC LIMIT 10"
                ).fetchall()
            ]
            coding_rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT re.content, co.outcome FROM raw_events re "
                    "LEFT JOIN coding_event_outcomes co ON co.event_id = re.id "
                    "WHERE re.source_type='coding_event' "
                    "  AND re.timestamp >= datetime('now', '-1 day') "
                    "ORDER BY re.timestamp DESC LIMIT 40"
                ).fetchall()
            ]
    except sqlite3.Error:
        contradictions, coding_rows = [], []

    focus_rows: list[dict] = []
    try:
        with autonomy_conn() as conn:
            focus_rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT content, category, created_at FROM focus_items "
                    "WHERE category IN ('focus', 'session_summary') "
                    "  AND created_at >= datetime('now', '-7 days') "
                    "ORDER BY created_at DESC LIMIT 20"
                ).fetchall()
            ]
    except sqlite3.Error:
        pass
    return contradictions, coding_rows, focus_rows


_SYNTH_PROMPT_HEADER = """You are brain's synthesis drive. You are NOT answering Chris's question — you generate observations AND commands brain wants to surface/enqueue.

Given Chris's last 24h of activity below, emit 0-2 short observations AND 0-1 action commands.

Observation = pattern Chris should notice (repeating, drifting, absent). One sentence, direct, no filler.

Command = concrete work item brain decides to delegate to an OpenClaw agent. ONLY emit a command when the pattern clearly calls for a specific action (not a discussion). Available agents:
- liz: engineering, code review, architecture, debugging, implementation
- ellie: infra, docker, nginx, homelab, automation
- sage: research, fact-checking, synthesis, profile regeneration
- jenna: chief of staff, scheduling, email, daily planning
- market: marketing/content

Language rule:
- Write each message in whichever single language (English or Korean) reads naturally for its content. If the message centers on English file/function/CLI names or technical paths, write the whole sentence in English. If it centers on Chris's intent, workflow, or product reasoning, Korean is fine.
- Do NOT translation-mix: avoid Korean grammar wrapping English keywords (e.g. "...에서 superseded돼서") — that reads broken. Pick one language per message.
- Keep each observation/command ≤ 140 characters so it fits one Telegram line without truncation.

Hard rules:
- Output JSON:
  {
    "observations": [{"severity": 3.0-8.0, "message": "<one sentence, ≤140 chars>", "category": "pattern"}],
    "commands": [{"to_agent": "<liz|ellie|sage|jenna|market>", "content": "<concrete work item, ≤140 chars>", "reason": "<short why>", "priority": 1-10}]
  }
- severity 3=FYI, 5=worth a look, 7=should check, 8=act today
- priority 1=urgent, 5=normal, 10=background
- If nothing worth saying/doing, return empty arrays. Fewer is better than generic.
- No prose outside the JSON. No markdown fences.
"""


def _build_prompt(contradictions: list[dict], coding_rows: list[dict], focus_rows: list[dict]) -> str:
    coding_lines = [
        f"[{(r.get('outcome') or 'pending')}] {(r.get('content') or '')[:200]}" for r in coding_rows[:30]
    ]
    contradiction_lines = [(r.get("summary") or "")[:180] for r in contradictions[:5]]
    focus_lines = [f"[{r.get('category')}] {(r.get('content') or '')[:150]}" for r in focus_rows[:10]]
    return (
        _SYNTH_PROMPT_HEADER
        + "\n=== Chris's recent focus / session summaries (7d) ===\n"
        + ("\n".join(focus_lines) or "(none)")
        + "\n\n=== Coding events (24h, with outcome) ===\n"
        + ("\n".join(coding_lines) or "(none)")
        + "\n\n=== New contradictions in attention (24h) ===\n"
        + ("\n".join(contradiction_lines) or "(none)")
        + "\n"
    )


def _dispatch(prompt: str) -> str | None:
    try:
        from cli_llm import dispatch as _llm

        result = _llm(
            agent="jenna",
            message=prompt,
            thinking="low",
            timeout=25,
            backlog_kind="synthesis",
            backlog_payload={"purpose": "brain_speak_synthesis_drive"},
        )
    except Exception as exc:
        log.debug("synthesis_drive dispatch raised: %s", exc)
        return None
    if not result or not getattr(result, "ok", False) or not result.text:
        return None
    text = (result.text or "").strip()
    text = _re.sub(r"^```(?:json)?\s*", "", text)
    return _re.sub(r"\s*```\s*$", "", text).strip()


def _parse(text: str) -> dict:
    try:
        return _json.loads(text)
    except (_json.JSONDecodeError, ValueError):
        log.debug("synthesis_drive parse failed: %s", text[:200])
        return {}


def _stable_topic_key(text: str) -> str | None:
    normalized = _re.sub(r"\s+", " ", (text or "").lower())
    if "openclaw" in normalized and any(
        marker in normalized for marker in ("무응답", "응답 정지", "응답정지", "no-response", "no response")
    ):
        return "openclaw_no_response_regression"
    if "지식 파이프라인" in normalized and any(
        marker in normalized for marker in ("모순", "프로필", "보존 정책")
    ):
        return "knowledge_pipeline_profile_policy_conflict"
    if "oc-lifehub" in normalized and any(
        marker in normalized for marker in ("pending", "superseded", "reverted", "회귀", "통합 체크")
    ):
        return "oc_lifehub_edit_thrash"
    return None


def _dedup_hash(text: str) -> str:
    topic = _stable_topic_key(text)
    if topic:
        return topic
    compact = _re.sub(r"\b20\d{2}[-./]\d{1,2}[-./]\d{1,2}\b", " ", text.lower())
    compact = _re.sub(r"\d+", " ", compact)
    compact = _re.sub(r"[^a-z가-힣]+", " ", compact)
    compact = _re.sub(r"\s+", " ", compact).strip()
    return hashlib.sha256(compact.encode()).hexdigest()[:12]


def _emit_observations(parsed: dict) -> list[Observation]:
    obs: list[Observation] = []
    for item in (parsed.get("observations") or [])[:2]:
        msg = str(item.get("message", "")).strip()
        if len(msg) < 10 or len(msg) > 280:
            continue
        sev = float(item.get("severity", 4.0))
        sev = max(3.0, min(8.0, sev))
        cat = str(item.get("category", "pattern"))[:32] or "pattern"
        dk = _dedup_hash(msg)
        obs.append(
            Observation(
                drive="synthesis_drive",
                category=cat,
                severity=sev,
                message=msg,
                dedup_key=f"synth:{dk}",
                payload={"source": "cli_llm"},
            )
        )
    return obs


_VALID_AGENTS = {"liz", "ellie", "sage", "jenna", "market"}


def _emit_commands(parsed: dict) -> list[Observation]:
    """Propose or dispatch commands. Gated by BRAIN_SYNTHESIS_AUTO_DISPATCH."""
    obs: list[Observation] = []
    for cmd in (parsed.get("commands") or [])[:1]:
        to_agent = str(cmd.get("to_agent", "")).lower().strip()
        content = str(cmd.get("content", "")).strip()
        reason = str(cmd.get("reason", "")).strip()
        priority = max(1, min(10, int(cmd.get("priority", 5))))
        if to_agent not in _VALID_AGENTS or len(content) < 15 or len(content) > 400:
            continue
        cmd_hash = _dedup_hash(f"{to_agent}|{content}")
        if was_sent_recently(f"cmd:{cmd_hash}", within_h=168):
            continue
        auto_enabled = _synthesis_auto_dispatch_enabled()
        if auto_enabled:
            try:
                from agent_messenger import send_message

                body = content + (f"\n\n[brain reasoning]: {reason}" if reason else "")
                send_message(
                    from_agent="brain",
                    to_agent=to_agent,
                    content=body,
                    message_type="task",
                    priority=priority,
                    metadata={"origin": "synthesis_drive", "reason": reason},
                )
            except Exception as exc:
                log.warning("synthesis_drive send_message failed: %s", exc)
                continue
            prefix = "→"
        else:
            prefix = "[제안]"
        # Word-boundary trim so the Telegram line doesn't end mid-syllable.
        summary = content if len(content) <= 160 else content[:160].rsplit(" ", 1)[0] + "…"
        obs.append(
            Observation(
                drive="synthesis_drive",
                category="command" if auto_enabled else "command_proposal",
                severity=4.5,
                message=f"{prefix} {to_agent}: {summary}",
                dedup_key=f"cmd:{cmd_hash}",
                payload={
                    "to_agent": to_agent,
                    "priority": priority,
                    "reason": reason,
                    "auto_dispatched": auto_enabled,
                },
            )
        )
    return obs


def synthesis_drive() -> list[Observation]:
    contradictions, coding_rows, focus_rows = _gather_signals()
    if not (contradictions or coding_rows or focus_rows):
        return []
    prompt = _build_prompt(contradictions, coding_rows, focus_rows)
    text = _dispatch(prompt)
    if not text:
        return []
    parsed = _parse(text)
    return _emit_observations(parsed) + _emit_commands(parsed)
