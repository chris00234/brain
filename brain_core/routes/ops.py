"""E1 session context + E2 TodoWrite + E4 skill discovery + F1 search
quality + code intelligence + tools discovery + accuracy/outcomes/procedures.

All thin wrappers over brain_core helpers. A shared sqlite helper
`_session_conn` is defined locally since multiple routes write to
autonomy.db::session_context.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Annotated

from api_deps import _safe_http_detail, verify_bearer
from config import BRAIN_DIR
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as PathParam
from pydantic import BaseModel, Field

router = APIRouter(dependencies=[Depends(verify_bearer)])


@contextmanager
def _session_conn():  # noqa: ANN202 — yield type sqlite3.Connection adds import weight
    import sqlite3

    db = BRAIN_DIR / "logs" / "autonomy.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS session_context (
                session_id TEXT NOT NULL,
                agent TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (session_id, agent, key)
            )
        """)
        yield conn
    finally:
        conn.close()


class SessionContextRequest(BaseModel):
    agent: str = Field(..., max_length=32)
    key: str = Field(..., min_length=1, max_length=100)
    value: str = Field(..., max_length=5000)


# ── Phase E1: Session context ──────────────────────────
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


# ── Phase D4: Session active agents ────────────────────
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


# ── Round 9: code intelligence ─────────────────────────
@router.get("/brain/code/find", tags=["brain"])
def code_find(
    q: str = Query(..., min_length=1, max_length=500),
    n: int = Query(default=10, ge=1, le=50),
) -> dict:
    """Search the code collection only — function-level results from indexed repos."""
    try:
        from search import get_collections, get_embedding, vector_search

        cols = get_collections()
        col_id = cols.get("code")
        if not col_id:
            return {
                "results": [],
                "error": "code collection not found — run /jobs/code_index_refresh first",
            }
        emb = get_embedding(q, prefix="query")
        data = vector_search(col_id, emb, n=n, query_text=q)
        ids = (data.get("ids") or [[]])[0]
        docs = (data.get("documents") or [[]])[0]
        metas = (data.get("metadatas") or [[]])[0]
        dists = (data.get("distances") or [[]])[0]
        results = [
            {
                "id": i,
                "score": round(max(0.0, 1 - float(dist)) * 100, 2),
                "file_path": (m or {}).get("file_path", ""),
                "function_name": (m or {}).get("function_name", ""),
                "signature": (m or {}).get("signature", ""),
                "language": (m or {}).get("language", ""),
                "line_start": (m or {}).get("line_start", 0),
                "snippet": (d or "")[:600],
            }
            for i, d, m, dist in zip(ids, docs, metas, dists, strict=False)
        ]
        return {"query": q, "total": len(results), "results": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


# ── Phase E2: TodoWrite sync ───────────────────────────
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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS todos (
                    session_id TEXT, idx INTEGER, content TEXT, status TEXT,
                    active_form TEXT, updated_at TEXT,
                    PRIMARY KEY (session_id, idx)
                )
            """)
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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS todos (
                    session_id TEXT, idx INTEGER, content TEXT, status TEXT,
                    active_form TEXT, updated_at TEXT,
                    PRIMARY KEY (session_id, idx)
                )
            """)
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


# ── Phase E4: Skill discovery ──────────────────────────
@router.get("/brain/skills", tags=["brain"])
def discover_skills(q: str = "", agent: str | None = None, limit: int = 20) -> dict:
    """Search OpenClaw + Claude Code skills via Neo4j skill graph."""
    try:
        from neo4j_client import run_query

        if q:
            rows = run_query(
                "MATCH (s:Skill) WHERE toLower(s.description) CONTAINS toLower($q) "
                "OR toLower(s.name) CONTAINS toLower($q) "
                "RETURN s.name AS name, s.description AS description, s.path AS path, "
                "  coalesce(s.use_count, 0) AS use_count "
                "ORDER BY use_count DESC, s.name ASC LIMIT $limit",
                {"q": q, "limit": limit},
            )
        else:
            rows = run_query(
                "MATCH (s:Skill) RETURN s.name AS name, s.description AS description, "
                "s.path AS path, coalesce(s.use_count, 0) AS use_count "
                "ORDER BY use_count DESC, s.name ASC LIMIT $limit",
                {"limit": limit},
            )
        return {"query": q, "total": len(rows), "skills": rows}
    except Exception as e:
        return {"query": q, "total": 0, "skills": [], "error": str(e)[:200]}


# ── Phase F1: Search quality dashboard ─────────────────
@router.get("/brain/search-quality", tags=["brain"])
def search_quality() -> dict:
    """Rolling search quality metrics for the Brain UI dashboard."""
    try:
        from metrics_buffer import metrics_buffer as _mb

        stats = _mb.search_latency_stats() if hasattr(_mb, "search_latency_stats") else {}
        feedback_file = BRAIN_DIR / "logs" / "search-feedback.jsonl"
        feedback_stats = {"useful": 0, "total": 0}
        if feedback_file.exists():
            try:
                with feedback_file.open() as f:
                    lines = f.readlines()[-500:]
                    for line in lines:
                        try:
                            d = json.loads(line)
                            feedback_stats["total"] += 1
                            if d.get("useful"):
                                feedback_stats["useful"] += 1
                        except Exception:  # noqa: S112 — skip malformed feedback
                            continue
            except Exception:  # noqa: S110 — optional feedback, never fatal
                pass
        return {
            "p50": stats.get("p50", 0),
            "p95": stats.get("p95", 0),
            "p99": stats.get("p99", 0),
            "count": stats.get("count", 0),
            "feedback": feedback_stats,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


# ── MCP tools discovery ────────────────────────────────
@router.get("/brain/tools", tags=["mcp"])
def brain_tools() -> dict:
    """MCP-compatible tool discovery — lists brain capabilities for external AI tools."""
    return {
        "tools": [
            {
                "name": "brain_recall",
                "description": "Search Chris's knowledge base (use brain_recall_v2 instead)",
                "endpoint": "GET /recall?q={query}&n={limit}",
                "deprecated": True,
            },
            {
                "name": "brain_recall_v2",
                "description": "Search with RRF fusion, reranking, graph, time decay.",
                "endpoint": "GET /recall/v2?q={query}&n={limit}",
            },
            {
                "name": "brain_store",
                "description": "Store a memory/fact/preference",
                "endpoint": "POST /memory",
            },
            {
                "name": "brain_decide",
                "description": "Get a preference-grounded decision recommendation",
                "endpoint": "POST /brain/decide",
            },
            {
                "name": "brain_reason",
                "description": "Deep multi-step reasoning with evidence",
                "endpoint": "POST /brain/reason",
            },
            {
                "name": "brain_ingest",
                "description": "Manually ingest a document or URL into the knowledge base",
                "endpoint": "POST /brain/ingest",
            },
            {
                "name": "brain_trace",
                "description": "Trace provenance/relation chains from a canonical note",
                "endpoint": "GET /brain/trace/{note_id}",
            },
            {
                "name": "brain_health",
                "description": "System health check",
                "endpoint": "GET /brain/health",
            },
            {
                "name": "brain_focus",
                "description": "Get/set working context",
                "endpoint": "GET/POST /brain/focus",
            },
            {
                "name": "brain_proactive",
                "description": "Current proactive insights and alerts",
                "endpoint": "GET /brain/proactive",
            },
        ]
    }


# ── Accuracy / outcomes / procedures ───────────────────
@router.get("/brain/accuracy", tags=["autonomy"])
def brain_accuracy(domain: str | None = None) -> dict:
    try:
        from brain_core.task_queue import task_queue

        return task_queue.get_domain_accuracy(domain=domain)
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.get("/brain/outcomes", tags=["autonomy"])
def brain_outcomes(domain: str | None = None, limit: int = 50, offset: int = 0) -> dict:
    try:
        from brain_core.task_queue import task_queue

        outcomes = task_queue.list_outcomes(domain=domain, limit=limit, offset=offset)
        return {"outcomes": outcomes, "total": len(outcomes)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.get("/brain/procedures", tags=["autonomy"])
def list_procedures(
    task_type: str | None = Query(default=None),
    source: str | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=50),
) -> dict:
    try:
        from brain_core.task_queue import task_queue

        procedures = task_queue.get_procedures(task_type=task_type, source=source, limit=limit)
        return {"procedures": procedures, "total": len(procedures)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"procedure query failed: {e}") from e
