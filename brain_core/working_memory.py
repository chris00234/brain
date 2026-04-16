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


_db_initialized = False

try:
    _init_db()
    _db_initialized = True
except Exception as _init_err:
    log.warning("working_memory _init_db failed (deferred to first use): %s", _init_err)


# ── Helpers ──────────────────────────────────────────────

from contextlib import contextmanager

@contextmanager
def _conn():
    global _db_initialized
    if not _db_initialized:
        _init_db()
        _db_initialized = True
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


# ── Session summary helpers ────────────────────────────────

SESSION_SUMMARY_CATEGORY = "session_summary"
MAX_SESSION_SUMMARIES = 5


def add_session_summary(content: str, agent: str = "claude", source: str = "session_auto") -> dict:
    """Store a session summary and evict oldest if over MAX_SESSION_SUMMARIES."""
    result = add_focus(
        content=content,
        category=SESSION_SUMMARY_CATEGORY,
        agent=agent,
        expires_hours=168,  # 1 week TTL
    )
    # Attach source in metadata
    with _conn() as conn:
        meta = json.dumps({"source": source})
        conn.execute(
            "UPDATE focus_items SET metadata = ? WHERE id = ?",
            (meta, result["id"]),
        )
    # Evict oldest beyond MAX_SESSION_SUMMARIES
    _evict_old_session_summaries()
    return result


def _evict_old_session_summaries() -> int:
    """Keep only the newest MAX_SESSION_SUMMARIES session summaries."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id FROM focus_items WHERE category = ? ORDER BY created_at DESC",
            (SESSION_SUMMARY_CATEGORY,),
        ).fetchall()
        to_delete = [r["id"] for r in rows[MAX_SESSION_SUMMARIES:]]
        if to_delete:
            placeholders = ",".join("?" for _ in to_delete)
            conn.execute(
                f"DELETE FROM focus_items WHERE id IN ({placeholders})",
                to_delete,
            )
        return len(to_delete)


def get_session_summaries(limit: int = MAX_SESSION_SUMMARIES) -> list[dict]:
    """Return the most recent session summaries, newest first."""
    _prune_expired()
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM focus_items "
            "WHERE category = ? AND (expires_at IS NULL OR expires_at >= ?) "
            "ORDER BY created_at DESC LIMIT ?",
            (SESSION_SUMMARY_CATEGORY, now_iso, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


# ── v3 session working memory (per-turn scratch, backed by session_context) ──
# Reuses the existing session_context table (session_id, agent, key, value,
# updated_at) that already lives in autonomy.db. This is the unified scratch
# buffer that replaces per-agent SCRATCH.md files. Agents read/write via the
# brain_wm_* MCP tools or the /brain/wm/* HTTP endpoints.
#
# "durable" flag: when a key is set with durable=True, wm_consolidate() (fired
# on SessionEnd) promotes it to the atoms truth layer before the session_context
# row is garbage-collected. That's how session learnings cross session boundaries.

_WM_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS session_context (
    session_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (session_id, agent, key)
);
"""


def _wm_ensure_schema() -> None:
    with _conn() as conn:
        conn.execute(_WM_TABLE_DDL)


def wm_set(
    session_id: str,
    agent: str,
    key: str,
    value: str,
    *,
    durable: bool = False,
) -> dict:
    """Upsert a session working-memory key. If durable=True, the key name is
    prefixed 'durable:' so wm_consolidate() can find it on SessionEnd."""
    _wm_ensure_schema()
    full_key = f"durable:{key}" if durable else key
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO session_context "
            "(session_id, agent, key, value, updated_at) VALUES (?,?,?,?,?)",
            (session_id, agent, full_key, value, now_iso),
        )
    return {
        "session_id": session_id,
        "agent": agent,
        "key": full_key,
        "durable": durable,
        "updated_at": now_iso,
    }


def wm_get(session_id: str, agent: str, key: str) -> str | None:
    """Return the value for (session_id, agent, key) or None."""
    _wm_ensure_schema()
    with _conn() as conn:
        # Try exact key first, then with durable: prefix
        row = conn.execute(
            "SELECT value FROM session_context WHERE session_id=? AND agent=? AND key=?",
            (session_id, agent, key),
        ).fetchone()
        if row:
            return row["value"]
        row = conn.execute(
            "SELECT value FROM session_context WHERE session_id=? AND agent=? AND key=?",
            (session_id, agent, f"durable:{key}"),
        ).fetchone()
        return row["value"] if row else None


def wm_list(session_id: str, agent: str) -> dict[str, dict]:
    """Return {key: {value, durable, updated_at}} for every key in the session
    belonging to the given agent."""
    _wm_ensure_schema()
    out: dict[str, dict] = {}
    with _conn() as conn:
        rows = conn.execute(
            "SELECT key, value, updated_at FROM session_context "
            "WHERE session_id=? AND agent=? ORDER BY updated_at DESC",
            (session_id, agent),
        ).fetchall()
    for r in rows:
        raw_key = r["key"]
        durable = raw_key.startswith("durable:")
        display_key = raw_key[len("durable:"):] if durable else raw_key
        out[display_key] = {
            "value": r["value"],
            "durable": durable,
            "updated_at": r["updated_at"],
        }
    return out


def wm_consolidate(session_id: str) -> int:
    """SessionEnd handler: promote durable:* rows to atoms (tier=episodic) and
    delete the rest. Called from post_session.sh.

    Returns the number of rows promoted.
    """
    _wm_ensure_schema()
    promoted = 0
    # HR3 fix (2026-04-14): use shared ingest_mirror so wm_consolidate
    # gets the full v3 Brain Hygiene pipeline. Previously durable
    # session rows got promoted to atoms with no hygiene fields.
    try:
        from ingest_mirror import mirror_memory
    except ImportError:
        mirror_memory = None

    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with _conn() as conn:
        rows = conn.execute(
            "SELECT agent, key, value, updated_at FROM session_context "
            "WHERE session_id=? AND key LIKE 'durable:%'",
            (session_id,),
        ).fetchall()

        for r in rows:
            if mirror_memory is None:
                continue
            try:
                chroma_id = f"session_wm:{session_id}:{r['agent']}:{r['key'][len('durable:'):]}"
                result = mirror_memory(
                    content=(r["value"] or "")[:2000],
                    chroma_id=chroma_id,
                    category="fact",
                    agent=r["agent"] or "unknown",
                    source=f"wm_consolidate:{session_id}",
                    operation="ADD",
                    confidence=0.7,
                    now_iso=now_iso,
                    allow_redistill=False,
                )
                if result.atom_id:
                    promoted += 1
            except Exception:
                continue

        # Delete all rows for this session (durable + ephemeral)
        conn.execute(
            "DELETE FROM session_context WHERE session_id=?",
            (session_id,),
        )
    return promoted


def wm_delete(session_id: str, agent: str, key: str) -> bool:
    """Remove a single session_context row. Returns True if deleted."""
    _wm_ensure_schema()
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM session_context WHERE session_id=? AND agent=? AND (key=? OR key=?)",
            (session_id, agent, key, f"durable:{key}"),
        )
        return cur.rowcount > 0
