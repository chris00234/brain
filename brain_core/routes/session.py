"""Session context (E1) + active-agents (D4) + TodoWrite sync (E2).

All three groups share the same autonomy.db sqlite store so they co-locate
with the `_session_conn` context manager.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Annotated

from api_deps import _safe_http_detail, verify_bearer
from config import BRAIN_DIR
from fastapi import APIRouter, Depends, HTTPException
from fastapi import Path as PathParam
from pydantic import BaseModel, Field

router = APIRouter(dependencies=[Depends(verify_bearer)])


@contextmanager
def _session_conn():  # noqa: ANN202 — sqlite3.Connection yield type adds import weight
    db = BRAIN_DIR / "logs" / "autonomy.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_context (
                session_id TEXT NOT NULL,
                agent TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (session_id, agent, key)
            )
            """
        )
        yield conn
    finally:
        conn.close()


class SessionContextRequest(BaseModel):
    agent: str = Field(..., max_length=32)
    key: str = Field(..., min_length=1, max_length=100)
    value: str = Field(..., max_length=5000)


# ── E1: session context ────────────────────────────────
@router.get("/brain/session/{session_id}/context", tags=["brain"])
def get_session_context(session_id: Annotated[str, PathParam()], agent: str | None = None) -> dict:
    try:
        with _session_conn() as conn:
            if agent:
                rows = conn.execute(
                    "SELECT agent, key, value, updated_at FROM session_context "
                    "WHERE session_id=? AND agent=?",
                    (session_id, agent),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT agent, key, value, updated_at FROM session_context WHERE session_id=?",
                    (session_id,),
                ).fetchall()
        return {
            "session_id": session_id,
            "total": len(rows),
            "items": [{"agent": r[0], "key": r[1], "value": r[2], "updated_at": r[3]} for r in rows],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.post("/brain/session/{session_id}/context", tags=["brain"])
def set_session_context(session_id: Annotated[str, PathParam()], req: SessionContextRequest) -> dict:
    try:
        with _session_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO session_context "
                "(session_id, agent, key, value, updated_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, req.agent, req.key, req.value, datetime.now(UTC).isoformat()),
            )
            conn.commit()
        return {"status": "ok", "session_id": session_id, "key": req.key}
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


# ── D4: active agents in a session ─────────────────────
@router.get("/brain/session/{session_id}/active_agents", tags=["coordination"])
def session_active_agents(session_id: Annotated[str, PathParam()]) -> dict:
    """Show which agents have context in this session and their latest keys."""
    try:
        with _session_conn() as conn:
            rows = conn.execute(
                "SELECT agent, key, value, updated_at FROM session_context "
                "WHERE session_id=? ORDER BY updated_at DESC",
                (session_id,),
            ).fetchall()
        by_agent: dict[str, list] = {}
        for agent, key, value, updated_at in rows:
            by_agent.setdefault(agent, []).append(
                {"key": key, "value": value[:200], "updated_at": updated_at}
            )
        return {
            "session_id": session_id,
            "active_agents": list(by_agent.keys()),
            "agent_count": len(by_agent),
            "contexts": by_agent,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


# ── E2: TodoWrite sync ─────────────────────────────────
class TodoItem(BaseModel):
    content: str
    status: str = "pending"
    activeForm: str | None = None


class TodoWriteRequest(BaseModel):
    todos: list[TodoItem]
    session_id: str | None = None


@router.post("/brain/todos", tags=["brain"])
def sync_todos(req: TodoWriteRequest) -> dict:
    """Sync TodoWrite state from Claude Code into brain."""
    try:
        with _session_conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS todos (
                    session_id TEXT, idx INTEGER, content TEXT, status TEXT,
                    active_form TEXT, updated_at TEXT,
                    PRIMARY KEY (session_id, idx)
                )
                """
            )
            now = datetime.now(UTC).isoformat()
            session = req.session_id or "default"
            conn.execute("DELETE FROM todos WHERE session_id=?", (session,))
            for i, t in enumerate(req.todos):
                conn.execute(
                    "INSERT INTO todos VALUES (?, ?, ?, ?, ?, ?)",
                    (session, i, t.content, t.status, t.activeForm, now),
                )
            conn.commit()
        return {"status": "ok", "count": len(req.todos), "session": session}
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.get("/brain/todos", tags=["brain"])
def get_todos(session_id: str = "default", status: str | None = None) -> dict:
    """Query todos by session, optionally filtered by status."""
    try:
        with _session_conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS todos (
                    session_id TEXT, idx INTEGER, content TEXT, status TEXT,
                    active_form TEXT, updated_at TEXT,
                    PRIMARY KEY (session_id, idx)
                )
                """
            )
            if status:
                rows = conn.execute(
                    "SELECT idx, content, status, active_form, updated_at FROM todos "
                    "WHERE session_id=? AND status=? ORDER BY idx",
                    (session_id, status),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT idx, content, status, active_form, updated_at FROM todos "
                    "WHERE session_id=? ORDER BY idx",
                    (session_id,),
                ).fetchall()
        return {
            "session_id": session_id,
            "total": len(rows),
            "todos": [
                {
                    "idx": r[0],
                    "content": r[1],
                    "status": r[2],
                    "activeForm": r[3],
                    "updated_at": r[4],
                }
                for r in rows
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e
