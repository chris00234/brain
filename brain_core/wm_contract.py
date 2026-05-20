"""brain_core/wm_contract.py — mandatory shared working-memory protocol.

Codex round-6 "10x" pick: every agent (Codex / Claude Code / OpenClaw / Hermes)
writes and reads the SAME five fields on every session so brain becomes the
hour-by-hour coordination spine instead of a nightly job collection.

The contract:
  - goal:         the user-facing outcome this session is driving toward
  - current_task: the specific thing the agent is doing RIGHT NOW
  - blocker:      anything preventing forward motion (empty string = unblocked)
  - decision:     the most recent non-trivial choice + 1-line reason
  - next_action:  the next step the agent intends to take

Storage: piggybacks on the existing ``working_memory.wm_set`` /
``working_memory.wm_get`` SQLite paths so the contract inherits the
``session_context`` retention policy. Keys are namespaced under
``contract:<field>`` so callers can still use freeform wm_set keys without
collision.

Boot-context: ``boot_context.py`` surfaces the contract in "Current Focus"
ahead of the generic working_memory dump so the first thing each agent reads
on session start is the protocol payload.

2026-05-20 W4 round-6: closes the gap where the round-3 dialectic system
made brain durable-store-grade but did nothing for live cross-agent
coordination during a working session.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

CONTRACT_FIELDS: tuple[str, ...] = (
    "goal",
    "current_task",
    "blocker",
    "decision",
    "next_action",
)

# Per-field length caps. Mirror working_memory.wm_set's 10000-char value
# limit but tighter so the contract stays readable in boot-context blocks.
FIELD_MAX_LEN: dict[str, int] = {
    "goal": 400,
    "current_task": 400,
    "blocker": 400,
    "decision": 600,
    "next_action": 400,
}

KEY_PREFIX = "contract:"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _validate(payload: dict) -> dict[str, str]:
    """Coerce inbound payload to the 5-field shape. Missing fields default to
    empty string; oversized values are truncated with a warning marker.
    """
    out: dict[str, str] = {}
    for field in CONTRACT_FIELDS:
        raw = payload.get(field, "")
        if raw is None:
            raw = ""
        if not isinstance(raw, str):
            raw = str(raw)
        cap = FIELD_MAX_LEN[field]
        if len(raw) > cap:
            raw = raw[: cap - 12] + "…[truncated]"
        out[field] = raw.strip()
    return out


def set_contract(session_id: str, agent: str, payload: dict) -> dict:
    """Write all 5 contract fields atomically (one wm_set per field).

    Returns ``{session_id, agent, fields, updated_at, missing_required}``.
    ``missing_required`` lists any of goal/current_task/next_action that the
    caller left empty — these aren't blocked (the contract is opt-in-full)
    but downstream signals can warn on them.
    """
    if not session_id or not agent:
        return {"error": "session_id + agent required"}
    import working_memory as _wm

    fields = _validate(payload)
    blob = json.dumps(fields, ensure_ascii=False)
    now = _now()
    _wm.wm_set(session_id, agent, f"{KEY_PREFIX}_blob", blob, durable=False)
    _wm.wm_set(session_id, agent, f"{KEY_PREFIX}_updated_at", now, durable=False)
    for name, value in fields.items():
        if value:
            _wm.wm_set(session_id, agent, f"{KEY_PREFIX}{name}", value, durable=False)

    missing_required = [n for n in ("goal", "current_task", "next_action") if not fields[n]]
    return {
        "session_id": session_id,
        "agent": agent,
        "fields": fields,
        "updated_at": now,
        "missing_required": missing_required,
    }


def get_contract(session_id: str, agent: str) -> dict:
    """Read the full contract payload. Returns empty fields for any missing
    keys so the response shape is always stable for clients.
    """
    if not session_id or not agent:
        return {"error": "session_id + agent required"}
    import working_memory as _wm

    blob = _wm.wm_get(session_id, agent, f"{KEY_PREFIX}_blob")
    updated_at = _wm.wm_get(session_id, agent, f"{KEY_PREFIX}_updated_at")
    fields: dict[str, str]
    if blob:
        try:
            parsed = json.loads(blob)
            fields = {f: str(parsed.get(f, ""))[: FIELD_MAX_LEN[f]] for f in CONTRACT_FIELDS}
        except Exception:
            fields = {f: "" for f in CONTRACT_FIELDS}
    else:
        # Backfill from per-field keys (an agent that wrote individual fields
        # without going through set_contract still gets surfaced).
        fields = {}
        for f in CONTRACT_FIELDS:
            v = _wm.wm_get(session_id, agent, f"{KEY_PREFIX}{f}")
            fields[f] = (v or "")[: FIELD_MAX_LEN[f]]
    return {
        "session_id": session_id,
        "agent": agent,
        "fields": fields,
        "updated_at": updated_at or "",
    }


def latest_contracts_for_agent(agent: str, limit: int = 2) -> list[dict]:
    """Cross-session: most-recent contract blobs this agent has written.

    boot_context calls this on session start to surface "what was I doing"
    across sessions, since the new session doesn't yet have a contract of
    its own. Pulls directly from session_context to bypass the per-session
    wm_get path.
    """
    if not agent:
        return []
    import sqlite3 as _sql

    from config import AUTONOMY_DB

    try:
        conn = _sql.connect(str(AUTONOMY_DB))
        conn.row_factory = _sql.Row
        try:
            rows = conn.execute(
                """
                SELECT session_id, value, updated_at
                FROM session_context
                WHERE agent = ? AND key = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (agent, f"{KEY_PREFIX}_blob", int(limit)),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    out: list[dict] = []
    for r in rows:
        try:
            fields = json.loads(r["value"] or "{}")
        except Exception:
            fields = {}
        out.append(
            {
                "session_id": r["session_id"],
                "agent": agent,
                "fields": {f: str(fields.get(f, ""))[: FIELD_MAX_LEN[f]] for f in CONTRACT_FIELDS},
                "updated_at": r["updated_at"] or "",
            }
        )
    return out


def render_for_boot(session_id: str | None, agent: str) -> str:
    """Markdown block for boot_context to surface in 'Current Focus'.

    When ``session_id`` is provided AND has a contract → render it.
    Otherwise fall back to the most recent N contracts the agent has
    written across sessions so the agent still sees its prior protocol
    state on a fresh boot.
    """
    if session_id:
        state = get_contract(session_id, agent)
        fields = state.get("fields") or {}
        if fields and any(fields.values()):
            lines = ["### Session Contract — brain/wm_contract"]
            if state.get("updated_at"):
                lines.append(f"_updated {state['updated_at']}_")
            for f in CONTRACT_FIELDS:
                v = fields.get(f) or "(not set)"
                lines.append(f"- **{f}**: {v}")
            return "\n".join(lines)

    history = latest_contracts_for_agent(agent, limit=2)
    if not history:
        return ""
    lines = ["### Recent Session Contracts — brain/wm_contract"]
    for entry in history:
        fields = entry.get("fields") or {}
        if not any(fields.values()):
            continue
        sid = (entry.get("session_id") or "")[:12]
        upd = entry.get("updated_at") or ""
        lines.append(f"- _session={sid} {upd}_")
        for f in CONTRACT_FIELDS:
            v = fields.get(f) or "(not set)"
            lines.append(f"  - **{f}**: {v}")
    return "\n".join(lines)
