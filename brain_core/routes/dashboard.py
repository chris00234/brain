"""Dashboard / introspection surface: code search + skill discovery +
search quality + MCP tool registry + accuracy/outcomes/procedures.

All endpoints are read-only thin wrappers over brain_core helpers. They
power the brain-ui dashboard and external MCP tool-discovery calls.
"""

from __future__ import annotations

import json

from api_deps import _safe_http_detail, verify_bearer
from fastapi import APIRouter, Depends, HTTPException, Query

from config import BRAIN_DIR

router = APIRouter(dependencies=[Depends(verify_bearer)])


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


# ── Phase E4: skill discovery ──────────────────────────
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


# ── Phase F1: search quality dashboard ─────────────────
@router.get("/brain/search-quality", tags=["brain"])
def search_quality() -> dict:
    """Rolling search quality metrics for the brain-ui dashboard."""
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


@router.get("/brain/judgment-report", tags=["brain"])
def judgment_report(hours: int = Query(default=24, ge=1, le=168)) -> dict:
    """Active-recall judgment telemetry for hook-noise and context-budget tuning."""
    try:
        from judgment_feedback import report

        return report(hours=hours)
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.get("/brain/judgment-tuning", tags=["brain"])
def judgment_tuning(
    hours: int = Query(default=24, ge=1, le=168),
    min_samples: int = Query(default=20, ge=5, le=500),
) -> dict:
    """Evidence-based active-recall policy recommendations; does not apply changes."""
    try:
        from judgment_feedback import tuning_report

        return tuning_report(hours=hours, min_samples=min_samples)
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


# ── MCP tool discovery ─────────────────────────────────
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
                "description": "Manually ingest a document or URL",
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
