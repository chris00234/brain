"""brain_core/agent_messenger.py — inter-agent message routing hub.

Brain as communication center. Agents send messages to each other;
routing logic decides whether to store, forward, escalate to Chris,
or create a task for the target agent.

Uses autonomy.db (shared with task_queue).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

try:
    from config import BRAIN_LOGS_DIR
except ImportError:
    BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")

DB_PATH = BRAIN_LOGS_DIR / "autonomy.db"

log = logging.getLogger("brain.agent_messenger")


# ── Schema ───────────────────────────────────────────────

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    content TEXT NOT NULL,
    message_type TEXT DEFAULT 'info',
    priority INTEGER DEFAULT 5,
    status TEXT DEFAULT 'pending',
    parent_task_id TEXT,
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    metadata TEXT DEFAULT '{}'
);
"""


def _init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_CREATE_TABLE)


_init_db()


# ── Helpers ──────────────────────────────────────────────


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


from contextlib import contextmanager


@contextmanager
def _conn():
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ── Public API ───────────────────────────────────────────


def send_message(
    from_agent: str,
    to_agent: str,
    content: str,
    message_type: str = "info",
    priority: int = 5,
    parent_task_id: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Insert a message and route it. Returns the message dict."""
    msg_id = uuid.uuid4().hex[:12]
    now = datetime.now(UTC).isoformat(timespec="seconds")
    meta_json = json.dumps(metadata or {}, ensure_ascii=False)

    with _conn() as conn:
        conn.execute(
            "INSERT INTO messages (id, from_agent, to_agent, content, message_type, "
            "priority, status, parent_task_id, created_at, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
            (msg_id, from_agent, to_agent, content, message_type, priority, parent_task_id, now, meta_json),
        )

    msg = {
        "id": msg_id,
        "from_agent": from_agent,
        "to_agent": to_agent,
        "content": content,
        "message_type": message_type,
        "priority": priority,
        "status": "pending",
        "parent_task_id": parent_task_id,
        "created_at": now,
        "delivered_at": None,
        "metadata": metadata or {},
    }

    action = route_message(msg)
    msg["_action"] = action
    return msg


def route_message(message: dict) -> str:
    """Decide immediate action for a message.

    Returns: 'forwarded' | 'escalated' | 'stored' | 'task_created'
    """
    mtype = message.get("message_type", "info")
    priority = message.get("priority", 5)
    from_agent = message.get("from_agent", "unknown")
    to_agent = message.get("to_agent", "unknown")
    content = message.get("content", "")

    # Decision messages need review, but most are LLM-handleable. Notify Chris
    # only when the policy detects missing private knowledge/authority.
    if mtype == "decision":
        if _requires_human_notification(message):
            _notify_chris(from_agent, to_agent, content)
            return "escalated"
        _handle_with_subscription_llm(from_agent, to_agent, content)
        return "forwarded"

    # Handoff: create a task for the target agent
    if mtype == "handoff":
        _create_handoff_task(message)
        return "task_created"

    # High-priority alerts are first reviewed by subscription LLM unless they
    # contain a concrete human-only blocker.
    if mtype == "alert" and priority <= 3:
        if _requires_human_notification(message):
            _notify_chris(from_agent, to_agent, content)
            return "escalated"
        _handle_with_subscription_llm(from_agent, to_agent, content)
        return "forwarded"

    # Everything else stays pending for the target agent's next boot
    return "stored"


def get_pending_messages(agent: str, limit: int = 10) -> list[dict]:
    """Fetch pending messages for an agent, ordered by priority ASC, created_at DESC."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE to_agent = ? AND status = 'pending' "
            "ORDER BY priority ASC, created_at DESC LIMIT ?",
            (agent, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def deliver_message(message_id: str) -> dict:
    """Mark a message as delivered. Returns updated message dict.

    Uses UPDATE rowcount as the authoritative not-found signal — a re-SELECT
    after the UPDATE could race with a concurrent delete or return stale data,
    so we key off "did the update touch any row" instead.
    """
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with _conn() as conn:
        cursor = conn.execute(
            "UPDATE messages SET status = 'delivered', delivered_at = ? WHERE id = ?",
            (now, message_id),
        )
        if cursor.rowcount == 0:
            return {"error": "not_found", "id": message_id}
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    if row is None:
        return {"error": "not_found", "id": message_id}
    return _row_to_dict(row)


def dismiss_all(agent: str) -> int:
    """Bulk-mark all pending messages for an agent as delivered. Returns count."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with _conn() as conn:
        count = conn.execute(
            "UPDATE messages SET status = 'delivered', delivered_at = ? "
            "WHERE to_agent = ? AND status = 'pending'",
            (now, agent),
        ).rowcount
    return count


# ── Internal ─────────────────────────────────────────────


def _requires_human_notification(message: dict) -> bool:
    """Return True only for blockers the LLM cannot resolve itself."""
    try:
        from escalation_policy import classify_escalation

        route = classify_escalation(
            title=(
                f"{message.get('message_type', 'message')} "
                f"{message.get('from_agent', '')}->{message.get('to_agent', '')}"
            ),
            content=message.get("content", ""),
            metadata=message.get("metadata") or {},
        )
        return route.notify_human
    except Exception as exc:
        log.debug("escalation policy unavailable, defaulting to LLM handling: %s", exc)
        return False


def _handle_with_subscription_llm(from_agent: str, to_agent: str, content: str) -> None:
    """Ask subscription-backed LLM to handle/review before Chris is notified."""
    try:
        from cli_llm import dispatch
        from escalation_policy import llm_review_prompt, llm_says_human_needed

        body = f"[AGENT MSG] {from_agent}\u2192{to_agent}: {content[:1200]}"
        result = dispatch(
            agent=to_agent or "sage",
            message=llm_review_prompt("agent_messenger", body),
            thinking="off",
            timeout=30,
            backlog_kind="proactive",
            backlog_payload={"source": "agent_messenger", "body": body},
        )
        if result.ok and llm_says_human_needed(result.text):
            _notify_chris(from_agent, to_agent, result.text[:1000])
    except Exception as exc:
        log.warning("subscription LLM escalation review failed: %s", exc)


def _notify_chris(from_agent: str, to_agent: str, content: str) -> None:
    """Send a Chris-facing notification without using an LLM path."""
    body = f"[AGENT MSG] {from_agent}\u2192{to_agent}: {content[:1000]}"
    try:
        from telegram_alert import send_chris_telegram

        send_chris_telegram(body, source="agent_messenger", severity="warn")
    except Exception as exc:
        log.warning("Chris notification dispatch failed: %s", exc)


def _escalate(from_agent: str, to_agent: str, content: str) -> None:
    """Back-compat alias for older callers/tests."""
    _notify_chris(from_agent, to_agent, content)


def _create_handoff_task(message: dict) -> None:
    """Create a task for the target agent via task_queue (if available)."""
    try:
        from task_queue import task_queue

        task_queue.create_task(
            title=f"Handoff from {message['from_agent']}: {message['content'][:120]}",
            description=message.get("content", ""),
            assigned_agent=message["to_agent"],
            parent_goal_id=message.get("parent_task_id"),
            metadata={
                "source": "agent_messenger",
                "source_message_id": message.get("id"),
                "from_agent": message.get("from_agent"),
                "message_type": message.get("message_type"),
            },
        )
    except Exception as exc:
        log.warning("handoff task creation failed (task_queue may not exist yet): %s", exc)
