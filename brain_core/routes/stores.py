"""Store-backed read/write endpoints: fact store, entity graph, failure
lessons, Claude Code session markers. All are thin wrappers over brain_core
modules with no shared server-side state.
"""

from __future__ import annotations

from api_deps import _safe_http_detail, verify_bearer
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(dependencies=[Depends(verify_bearer)])


# ── Fact store ─────────────────────────────────────────
@router.get("/brain/facts", tags=["facts"])
def facts_query(entity: str | None = None, attribute: str | None = None, limit: int = 50) -> dict:
    try:
        from fact_store import query_facts

        return {"facts": query_facts(entity=entity, attribute=attribute, limit=limit)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


class FactStoreRequest(BaseModel):
    entity: str = Field(..., min_length=1, max_length=200)
    attribute: str = Field(..., min_length=1, max_length=200)
    value: str = Field(..., min_length=1, max_length=2000)
    source: str = ""
    source_type: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    valid_from: str = ""
    valid_to: str = ""


@router.post("/brain/facts", tags=["facts"])
def facts_store(req: FactStoreRequest) -> dict:
    try:
        from fact_store import store_fact

        return store_fact(
            entity=req.entity,
            attribute=req.attribute,
            value=req.value,
            source=req.source,
            source_type=req.source_type,
            confidence=req.confidence,
            valid_from=req.valid_from,
            valid_to=req.valid_to,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.get("/brain/facts/entity/{entity_name}", tags=["facts"])
def facts_by_entity(entity_name: str) -> dict:
    try:
        from fact_store import get_entity_facts

        return {"entity": entity_name, "facts": get_entity_facts(entity_name)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.get("/brain/facts/stats", tags=["facts"])
def facts_stats() -> dict:
    try:
        from fact_store import stats

        return stats()
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


# ── Entity graph ───────────────────────────────────────
@router.get("/brain/graph/stats", tags=["graph"])
def graph_stats_endpoint() -> dict:
    try:
        from brain_core.entity_graph import get_graph_stats

        return get_graph_stats()
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


# ── Qdrant parity stats ────────────────────────────────
@router.get("/brain/qdrant/stats", tags=["qdrant"])
def qdrant_stats_endpoint() -> dict:
    """Per-collection point counts. Parity with /brain/graph/stats."""
    try:
        from vector_store import get_vector_store

        store = get_vector_store()
        if not store.heartbeat():
            return {"backend": "unavailable", "collections": {}}
        collections: dict[str, int] = {}
        for name in store.list_collections():
            try:
                collections[name] = int(store.count(name))
            except Exception:
                collections[name] = -1
        total = sum(c for c in collections.values() if c >= 0)
        return {
            "backend": "qdrant",
            "collection_count": len(collections),
            "total_points": total,
            "collections": collections,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.get("/brain/graph/nodes", tags=["graph"])
def graph_nodes_endpoint(limit: int = 200, connected_only: bool = False) -> dict:
    """Entities + relations for 3D graph visualization.

    Links are restricted to pairs whose BOTH endpoints made the top-limit
    nodes list — prevents UI isolated-node artifacts where a node appears
    but its relations reference off-canvas entities.
    """
    try:
        from brain_core.neo4j_client import is_healthy, run_query

        if not is_healthy():
            return {"nodes": [], "links": [], "backend": "unavailable"}
        nodes = run_query(
            "MATCH (e:Entity) RETURN e.id AS id, e.name AS name, "
            "coalesce(e.entity_type, 'concept') AS type, "
            "coalesce(e.mention_count, 1) AS mention_count, "
            "coalesce(e.memory_class, 'ephemeral') AS memory_class "
            "ORDER BY e.mention_count DESC LIMIT $limit",
            {"limit": limit},
        )
        node_ids = [n["id"] for n in nodes]
        links = run_query(
            "MATCH (s:Entity)-[r:RELATES_TO]->(t:Entity) "
            "WHERE s.id IN $ids AND t.id IN $ids "
            "RETURN s.id AS source, t.id AS target, "
            "coalesce(r.relationship, 'related_to') AS relationship, "
            "coalesce(r.weight, 0.5) AS weight "
            "ORDER BY r.weight DESC",
            {"ids": node_ids},
        )
        if connected_only:
            linked: set = set()
            for link in links:
                linked.add(link["source"])
                linked.add(link["target"])
            nodes = [n for n in nodes if n["id"] in linked]
        return {"nodes": nodes, "links": links, "backend": "neo4j"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


# ── Failure lessons ────────────────────────────────────
@router.get("/brain/lessons", tags=["brain"])
def get_lessons(agent: str = "system", limit: int = 20) -> dict:
    """Query failure lessons for an agent."""
    try:
        import failure_memory

        lessons = failure_memory.get_similar_lessons("", agent_id=agent, limit=limit)
        return {"agent": agent, "total": len(lessons), "lessons": lessons}
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


# ── Claude Code session markers ────────────────────────
@router.post("/brain/claude-session/start", tags=["brain"])
def claude_session_start(session_id: str = "") -> dict:
    """Mark a Claude Code session as active. Called by SessionStart hook."""
    from brain_core import claude_session

    return claude_session.start_session(session_id)


@router.post("/brain/claude-session/heartbeat", tags=["brain"])
def claude_session_heartbeat() -> dict:
    """Extend the active session TTL."""
    from brain_core import claude_session

    return claude_session.extend_session()


@router.post("/brain/claude-session/end", tags=["brain"])
def claude_session_end() -> dict:
    """Clear the session marker. Called by SessionEnd hook."""
    from brain_core import claude_session

    return claude_session.end_session()
