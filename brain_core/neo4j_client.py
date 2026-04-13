"""brain_core/neo4j_client.py — Neo4j Bolt connection wrapper.

Singleton driver with lazy init. Used by entity_graph.py for graph operations.
Falls back gracefully — callers should catch exceptions.

Usage:
    from neo4j_client import run_query, is_healthy, ensure_schema
"""

from __future__ import annotations

import logging
import threading
from typing import Any

log = logging.getLogger("brain.neo4j_client")

try:
    from config import NEO4J_BOLT_URI
except ImportError:
    NEO4J_BOLT_URI = "bolt://127.0.0.1:7687"

_driver = None
_driver_lock = threading.Lock()


def get_driver():
    global _driver
    if _driver is not None:
        return _driver
    with _driver_lock:
        if _driver is None:
            from neo4j import GraphDatabase
            _driver = GraphDatabase.driver(
                NEO4J_BOLT_URI,
                auth=None,
                max_connection_pool_size=8,
                max_connection_lifetime=3600,  # 1 hour
                connection_acquisition_timeout=30,
            )
        return _driver


def run_query(cypher: str, params: dict[str, Any] | None = None) -> list[dict]:
    """Execute a read Cypher query, return list of record dicts."""
    def _work(tx):
        result = tx.run(cypher, params or {})
        return [dict(record) for record in result]
    with get_driver().session() as session:
        return session.execute_read(_work)


def run_write(cypher: str, params: dict[str, Any] | None = None) -> None:
    """Execute a write Cypher query (no return needed)."""
    def _work(tx):
        tx.run(cypher, params or {}).consume()
    with get_driver().session() as session:
        session.execute_write(_work)


def is_healthy() -> bool:
    """Check Neo4j connectivity. Resets driver on failure for clean reconnect."""
    global _driver
    try:
        get_driver().verify_connectivity()
        return True
    except Exception:
        old_driver = None
        with _driver_lock:
            old_driver = _driver
            _driver = None
        if old_driver:
            try:
                old_driver.close()
            except Exception:
                pass
        return False


def ensure_schema() -> None:
    """Create constraints + indexes (idempotent)."""
    schema_statements = [
        "CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (e:Entity) REQUIRE e.id IS UNIQUE",
        "CREATE INDEX entity_name IF NOT EXISTS FOR (e:Entity) ON (e.name)",
        "CREATE INDEX entity_memory_class IF NOT EXISTS FOR (e:Entity) ON (e.memory_class)",
        "CREATE CONSTRAINT memory_access_id IF NOT EXISTS FOR (m:MemoryAccess) REQUIRE m.memory_id IS UNIQUE",
        "CREATE INDEX memory_access_ts IF NOT EXISTS FOR (m:MemoryAccess) ON (m.last_accessed_at)",
    ]
    with get_driver().session() as session:
        for stmt in schema_statements:
            try:
                session.run(stmt)
            except Exception as e:
                log.debug("schema statement skipped: %s", e)


def get_stats() -> dict:
    """Return node/relation counts for monitoring."""
    try:
        entities = run_query("MATCH (e:Entity) RETURN count(e) AS c")[0]["c"]
        relations = run_query("MATCH ()-[r]->() RETURN count(r) AS c")[0]["c"]
        access = run_query("MATCH (m:MemoryAccess) RETURN count(m) AS c, coalesce(sum(m.access_count), 0) AS total")[0]
        return {
            "entities": entities,
            "relations": relations,
            "tracked_memories": access["c"],
            "total_accesses": access["total"],
        }
    except Exception as e:
        return {"error": str(e)}
