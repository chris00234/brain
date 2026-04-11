"""brain_core/working_memory.py — persistent working context.

Answers "what is Chris focused on right now?" by aggregating signals
from task_queue goals, blocked tasks, pending agent messages, and
manually-set focus items.

Uses autonomy.db (shared with task_queue, agent_messenger).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

try:
    from config import BRAIN_LOGS_DIR
except ImportError:
    BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")

DB_PATH = BRAIN_LOGS_DIR / "autonomy.db"

log = logging.getLogger("brain.working_memory")


# ── Schema ───────────────────────────────────────────────

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS focus_items (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    category TEXT DEFAULT 'focus',
    agent TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    metadata TEXT DEFAULT '{}'
);
"""


def _init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA cache_size=-8000")
        conn.execute(_CREATE_TABLE)


_init_db()


# ── Helpers ──────────────────────────────────────────────

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


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _prune_expired() -> int:
    """Delete focus items past their expiration. Returns count deleted."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM focus_items WHERE expires_at IS NOT NULL AND expires_at < ?",
            (now,),
        )
        return cur.rowcount


# ── Public API ───────────────────────────────────────────

def get_working_context() -> dict:
    """Build a snapshot of Chris's current working context from all signals."""

    # ── Active goals with progress ───────────────────────
    active_goals: list[dict] = []
    try:
        from task_queue import task_queue
        for goal in task_queue.list_goals(status="active"):
            progress = task_queue.get_goal_progress(goal["id"])
            active_goals.append({
                "id": goal["id"],
                "title": goal["title"],
                "progress_pct": progress.get("pct", 0),
                "summary": f"{progress.get('completed', 0)}/{progress.get('total', 0)} done",
            })
    except Exception as exc:
        log.debug("task_queue unavailable for goals: %s", exc)

    # ── Blocked tasks (assigned but deps not met) ────────
    blocked: list[dict] = []
    try:
        from task_queue import task_queue
        for task in task_queue.list_tasks(status="assigned"):
            deps = task.get("depends_on", [])
            if isinstance(deps, str):
                deps = json.loads(deps)
            if not deps:
                continue
            for dep_id in deps:
                dep = task_queue.get_task(dep_id)
                if dep and dep["status"] != "completed":
                    blocked.append({
                        "task_id": task["id"],
                        "title": task["title"],
                        "blocked_by": dep_id,
                        "agent": task["assigned_agent"],
                    })
                    break
    except Exception as exc:
        log.debug("task_queue unavailable for blocked scan: %s", exc)

    # ── Next up (approved, deps met) ─────────────────────
    next_up: list[dict] = []
    try:
        from task_queue import task_queue
        for t in task_queue.get_ready_tasks()[:5]:
            next_up.append({
                "task_id": t["id"],
                "title": t["title"],
                "agent": t["assigned_agent"],
            })
    except Exception as exc:
        log.debug("task_queue unavailable for ready tasks: %s", exc)

    # ── Open threads (pending messages across agents) ────
    open_threads: list[dict] = []
    try:
        from agent_messenger import get_pending_messages
        all_agents = ["liz", "ellie", "jenna", "sage", "market", "claude"]
        now_utc = datetime.now(timezone.utc)
        for agent in all_agents:
            for m in get_pending_messages(agent, limit=3):
                try:
                    created = datetime.fromisoformat(m["created_at"])
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    age_hours = (now_utc - created).total_seconds() / 3600
                except (ValueError, KeyError):
                    age_hours = 0
                open_threads.append({
                    "from": m["from_agent"],
                    "to": m["to_agent"],
                    "summary": m["content"][:100],
                    "age_hours": round(age_hours, 1),
                })
    except Exception as exc:
        log.debug("agent_messenger unavailable: %s", exc)

    # ── Manual focus items ───────────────────────────────
    _prune_expired()
    manual_focus: list[dict] = []
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM focus_items "
            "WHERE (expires_at IS NULL OR expires_at >= ?) "
            "ORDER BY created_at DESC",
            (now_iso,),
        ).fetchall()
    manual_focus = [_row_to_dict(r) for r in rows]

    return {
        "active_goals": active_goals[:3],
        "blocked": blocked[:2],
        "next_up": next_up[:3],
        "open_threads": open_threads[:3],
        "manual_focus": manual_focus[:2],
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def add_focus(
    content: str,
    category: str = "focus",
    agent: str | None = None,
    expires_hours: int = 168,
    thread_id: str | None = None,
) -> dict:
    """Add a manual focus item. Default TTL is 1 week (168h).
    thread_id links related work across sessions (LangGraph/Letta pattern)."""
    item_id = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc)
    created = now.isoformat(timespec="seconds")
    expires = (now + timedelta(hours=expires_hours)).isoformat(timespec="seconds")
    meta = {"thread_id": thread_id} if thread_id else {}
    meta_json = json.dumps(meta)

    with _conn() as conn:
        conn.execute(
            "INSERT INTO focus_items (id, content, category, agent, created_at, expires_at, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (item_id, content, category, agent, created, expires, meta_json),
        )

    return {
        "id": item_id,
        "content": content,
        "category": category,
        "agent": agent,
        "created_at": created,
        "expires_at": expires,
        "metadata": {},
    }


def remove_focus(focus_id: str) -> bool:
    """Delete a focus item by id. Returns True if a row was removed."""
    with _conn() as conn:
        cur = conn.execute("DELETE FROM focus_items WHERE id = ?", (focus_id,))
        return cur.rowcount > 0
