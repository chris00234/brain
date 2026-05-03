"""brain_core/entity_graph.py — entity-relationship graph with Neo4j + SQLite fallback.

Primary backend: Neo4j Community (bolt://127.0.0.1:7687)
Fallback: SQLite tables in autonomy.db (if Neo4j is down)

Entity extraction happens via Sage (cheap, thinking=low) on memory writes.
1-hop expansion at retrieval time surfaces connected knowledge.
Memory access tracking enables lifecycle management (stale memory detection).

Usage:
    from entity_graph import extract_and_store_entities, expand_with_entities
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

log = logging.getLogger("brain.entity_graph")

DB_PATH = BRAIN_LOGS_DIR / "autonomy.db"

# ---------------------------------------------------------------------------
# SQLite fallback schema (kept as cold backup even when Neo4j is primary)
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    entity_type TEXT DEFAULT 'concept',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    mention_count INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS entity_relations (
    id TEXT PRIMARY KEY,
    source_entity TEXT NOT NULL REFERENCES entities(id),
    relationship TEXT NOT NULL,
    target_entity TEXT NOT NULL REFERENCES entities(id),
    confidence REAL DEFAULT 0.5,
    created_at TEXT NOT NULL,
    source_memory_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rel_unique ON entity_relations(source_entity, relationship, target_entity);
CREATE INDEX IF NOT EXISTS idx_entity_name ON entities(name);
CREATE INDEX IF NOT EXISTS idx_rel_source ON entity_relations(source_entity);
CREATE INDEX IF NOT EXISTS idx_rel_target ON entity_relations(target_entity);
CREATE TABLE IF NOT EXISTS memory_access (
    memory_id TEXT PRIMARY KEY,
    access_count INTEGER DEFAULT 0,
    last_accessed_at TEXT NOT NULL,
    first_accessed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_access_last ON memory_access(last_accessed_at);
"""


def _init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 2026-04-18: sqlite3's `with` commits/rolls back but does NOT close the
    # connection. If any statement below raises (e.g. UNIQUE index creation
    # with residual duplicates), the connection leaks for the process
    # lifetime — and this is called at module import time, so the leak
    # can never be reclaimed. Use try/finally to guarantee close.
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        # Create base tables first (no UNIQUE index yet)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS entities (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                entity_type TEXT DEFAULT 'concept',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                mention_count INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS entity_relations (
                id TEXT PRIMARY KEY,
                source_entity TEXT NOT NULL REFERENCES entities(id),
                relationship TEXT NOT NULL,
                target_entity TEXT NOT NULL REFERENCES entities(id),
                confidence REAL DEFAULT 0.5,
                created_at TEXT NOT NULL,
                source_memory_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_entity_name ON entities(name);
            CREATE INDEX IF NOT EXISTS idx_rel_source ON entity_relations(source_entity);
            CREATE INDEX IF NOT EXISTS idx_rel_target ON entity_relations(target_entity);
            CREATE TABLE IF NOT EXISTS memory_access (
                memory_id TEXT PRIMARY KEY,
                access_count INTEGER DEFAULT 0,
                last_accessed_at TEXT NOT NULL,
                first_accessed_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_memory_access_last ON memory_access(last_accessed_at);
            """
        )
        # Dedup entity_relations before creating the UNIQUE index. Legacy rows
        # were inserted without the uniqueness guarantee; delete everything but
        # the highest-confidence / latest row per (source, rel, target).
        # Bug fix 2026-04-12: every _init_db call was failing UNIQUE constraint
        # creation because the data had duplicates.
        try:
            conn.execute(
                """
                DELETE FROM entity_relations
                WHERE id NOT IN (
                    SELECT id FROM (
                        SELECT id,
                               ROW_NUMBER() OVER (
                                   PARTITION BY source_entity, relationship, target_entity
                                   ORDER BY confidence DESC, created_at DESC, id DESC
                               ) AS rn
                        FROM entity_relations
                    )
                    WHERE rn = 1
                )
                """
            )
        except sqlite3.Error:
            pass
        # Now safe to create the UNIQUE index
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_rel_unique ON entity_relations(source_entity, relationship, target_entity)"
        )
        conn.commit()
    finally:
        conn.close()


try:
    _init_db()
except Exception as _e:
    log.warning("entity_graph _init_db failed (non-fatal): %s", _e)


def _conn():
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA cache_size=-8000")
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Neo4j availability check (cached for 60s)
# ---------------------------------------------------------------------------

_neo4j_available: bool | None = None
_neo4j_checked_at: float = 0.0
_NEO4J_CHECK_TTL = 300.0  # 5 min — reduces flip-flop risk during brief hiccups

import threading as _threading
import time as _time

_neo4j_lock = _threading.Lock()


def _use_neo4j() -> bool:
    global _neo4j_available, _neo4j_checked_at
    now = _time.time()
    with _neo4j_lock:
        if _neo4j_available is not None and (now - _neo4j_checked_at) < _NEO4J_CHECK_TTL:
            return _neo4j_available
        try:
            from neo4j_client import is_healthy

            _neo4j_available = is_healthy()
        except Exception:
            _neo4j_available = False
        _neo4j_checked_at = _time.time()
        if not _neo4j_available:
            log.debug("Neo4j unavailable, using SQLite fallback")
        return _neo4j_available


# ---------------------------------------------------------------------------
# Public API — Neo4j primary, SQLite fallback
# ---------------------------------------------------------------------------


def extract_and_store_entities(
    memory_content: str,
    memory_id: str = "",
    *,
    timeout: int = 30,
    max_backends: int | None = None,
) -> int:
    """Extract entities + relations from memory content via Sage, store in graph.

    Return values:
      >= 0 — success (may be 0 if Sage returned no entities for short/opaque
             content, or if all relations were malformed)
      -1   — LLM dispatch failed (rate-limited, breaker open, timeout,
             parse error). Caller should treat as "retryable" — this is
             the signal the llm_backlog handler uses to keep the entry
             pending instead of marking it done (CR8 fix 2026-04-14).

    Text shorter than 30 chars returns 0 (nothing to extract) — NOT -1,
    since there's no LLM work to retry for stubs.
    """
    if len(memory_content) < 30:
        return 0

    try:
        from cli_llm import dispatch

        prompt = (
            f"Extract entities and relationships from this text.\n\n"
            f"Text: {memory_content[:1000]}\n\n"
            f"Return ONLY a JSON object:\n"
            f'{{"entities": [{{"name": "...", "type": "person|service|tool|concept|project|decision"}}], '
            f'"relations": [{{"source": "...", "relationship": "uses|depends_on|created|manages|part_of|related_to", "target": "..."}}]}}\n\n'
            f"Max 5 entities, 5 relations. Only extract clearly stated facts."
        )
        # NOTE: tried session_id="brain_mech_sage" isolation — OpenClaw
        # silently remaps unknown string IDs to the agent's main session,
        # so isolation requires valid UUID format + deeper OpenClaw config
        # work. Leaving main-session routing until that's investigated.
        result = dispatch(
            agent="sage",
            message=prompt,
            thinking="low",
            timeout=timeout,
            max_backends=max_backends,
        )
        # CR8 fix: distinguish LLM failure from "LLM ran, no entities".
        # result.ok=False means transport/rate-limit/breaker — retryable.
        if not result.ok:
            return -1
        if not result.text:
            return -1  # empty response also counts as retryable failure

        import re

        text = result.text.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        try:
            data = json.loads(text.strip())
        except json.JSONDecodeError:
            # Parse error with a non-empty response = LLM confused, not
            # degraded. Mark done so we don't retry forever on bad output.
            return 0

        entities = data.get("entities", [])
        relations = data.get("relations", [])
        if not entities:
            return 0

        now = _now()
        _ontology_validate_extracted_relations(entities, relations, memory_id or "entity_graph.extract")

        if _use_neo4j():
            created = _neo4j_store_entities(entities, relations, now, memory_id)
        else:
            created = _sqlite_store_entities(entities, relations, now, memory_id)

        # Phase N4: mirror every extracted entity into brain.db atom_entity so
        # the join table populates even when the atoms truth layer is the
        # consumer (sleep_consolidate, downstream retrieval). Gated by
        # BRAIN_ATOMS_ENABLED — no-op when atoms disabled.
        try:
            from atoms_store import (
                BRAIN_ATOMS_ENABLED as _ae,
            )
            from atoms_store import (
                derive_atom_id as _dai,
            )
            from atoms_store import (
                link_atom_entity as _lae,
            )
            from atoms_store import (
                upsert_entity as _ue,
            )

            if _ae and memory_id:
                atom_id = _dai(memory_id)
                for ent in entities[:5]:
                    name = (ent.get("name") or "").strip().lower()
                    etype = (ent.get("type") or "concept").lower()
                    if not name or len(name) < 2 or len(name) > 50:
                        continue
                    eid = _ue(name, etype)
                    if eid:
                        _lae(atom_id, eid, role="subject")
        except Exception as _exc:
            log.debug("atom_entity mirror failed: %s", _exc)

        return created

    except Exception as e:
        log.debug("entity extraction failed: %s", e)
        # CR8: exception during the LLM call or Neo4j write = retryable.
        # Backlog handler will requeue. Import errors / config errors
        # obviously aren't retryable but we can't distinguish cleanly;
        # 5 retries in backlog caps the cost either way.
        return -1


from collections import OrderedDict as _OrderedDict

_expand_cache: _OrderedDict[str, tuple[float, list[str]]] = _OrderedDict()
_expand_cache_lock = _threading.Lock()
_EXPAND_CACHE_TTL = 300.0  # 5 minutes
_EXPAND_CACHE_MAX = 256

# Phase G3: speaker-relation exclusion set cache. Powers the
# /recall/v2?exclude_already_used=true filter so tool-recommendation queries
# can drop candidates that Chris already (uses|owns|installed). Keyed
# (speaker, relationship); 5 min TTL matches _expand_cache.
_excluded_cache: _OrderedDict[str, tuple[float, set[str]]] = _OrderedDict()
_excluded_cache_lock = _threading.Lock()
_EXCLUDED_CACHE_TTL = 300.0
_EXCLUDED_CACHE_MAX = 64


def get_excluded_entities(speaker: str = "chris", relationship: str = "uses") -> set[str]:
    """Phase G3: canonical names where (speaker)-[:RELATES_TO {relationship}]->(target).

    Used by /recall/v2?exclude_already_used=true to filter recommendations
    against the graph of facts the brain already knows about Chris's stack.
    Returns lowercased names so callers can do case-insensitive comparison
    without re-normalizing per-result.

    Falls back to an empty set on Neo4j unavailable or query error — the
    feature is opt-in, so empty set just means "no filter applied".
    """
    cache_key = f"{(speaker or '').lower()}:{(relationship or '').lower()}"
    if not cache_key.strip(":"):
        return set()
    now = _time.time()
    with _excluded_cache_lock:
        cached = _excluded_cache.get(cache_key)
        if cached and (now - cached[0]) < _EXCLUDED_CACHE_TTL:
            return set(cached[1])

    result: set[str] = set()
    if _use_neo4j():
        try:
            from neo4j_client import run_query

            rows = run_query(
                "MATCH (s:Entity)-[r:RELATES_TO {relationship: $rel}]->(t:Entity) "
                "WHERE toLower(s.name) = toLower($speaker) "
                "RETURN DISTINCT toLower(t.name) AS name",
                {"speaker": speaker, "rel": relationship},
            )
            result = {row["name"] for row in rows if row.get("name")}
        except Exception as exc:
            log.debug("get_excluded_entities cypher failed: %s", exc)

    with _excluded_cache_lock:
        _excluded_cache[cache_key] = (now, result)
        _excluded_cache.move_to_end(cache_key)
        if len(_excluded_cache) > _EXCLUDED_CACHE_MAX:
            _excluded_cache.popitem(last=False)
    return set(result)


def _ontology_validate_extracted_relations(entities: list, relations: list, origin: str) -> dict[str, int]:
    """Warning-only validation for LLM-extracted graph edges.

    Central ontology semantics live in ontology.py. This helper only adapts
    entity_graph's extracted JSON shape to that registry and never blocks writes.
    """
    if not relations:
        return {}
    try:
        from ontology import RelationRecord, issue_summary, validate_relation
    except Exception as exc:
        log.debug("ontology validation unavailable: %s", exc)
        return {}

    entity_types = {
        (ent.get("name") or "").strip().lower(): (ent.get("type") or "concept")
        for ent in entities[:5]
        if isinstance(ent, dict)
    }
    issues = []
    for rel in relations[:5]:
        if not isinstance(rel, dict):
            continue
        src = (rel.get("source") or "").strip().lower()
        tgt = (rel.get("target") or "").strip().lower()
        issues.extend(
            validate_relation(
                RelationRecord(
                    source=src,
                    source_type=entity_types.get(src, ""),
                    relation=rel.get("relationship", "related_to"),
                    target=tgt,
                    target_type=entity_types.get(tgt, ""),
                    origin=origin,
                )
            )
        )
    summary = issue_summary(issue for issue in issues if issue.severity != "info")
    if summary:
        log.warning("ontology graph validation warnings origin=%s summary=%s", origin, summary)
    else:
        info_summary = issue_summary(issues)
        if info_summary:
            log.debug("ontology graph validation info origin=%s summary=%s", origin, info_summary)
    return summary


def resolve_entity(name: str, entity_type: str | None = None) -> str | None:
    """Check if name is a known alias of an existing entity. Returns canonical name or None.
    If entity_type is provided, only match entities of that type (prevents agent/pet/person collisions)."""
    if not _use_neo4j():
        return None
    try:
        from neo4j_client import run_query

        type_clause = "AND e.entity_type = $type " if entity_type else ""
        result = run_query(
            "MATCH (e:Entity) "
            "WHERE (toLower(e.name) = toLower($name) "
            "   OR toLower($name) IN [a IN coalesce(e.aliases, []) | toLower(a)]) "
            f"{type_clause}"
            "RETURN e.name AS canonical_name "
            "LIMIT 1",
            {"name": name, "type": entity_type},
        )
        return result[0]["canonical_name"] if result else None
    except Exception:
        return None


def add_alias(entity_name: str, alias: str) -> bool:
    """Add an alias to an existing entity node."""
    if not _use_neo4j():
        return False
    try:
        from neo4j_client import run_write

        run_write(
            "MATCH (e:Entity {name: $name}) "
            "SET e.aliases = CASE "
            "  WHEN e.aliases IS NULL THEN [$alias] "
            "  WHEN NOT toLower($alias) IN [a IN e.aliases | toLower(a)] "
            "    THEN e.aliases + $alias "
            "  ELSE e.aliases END",
            {"name": entity_name, "alias": alias},
        )
        return True
    except Exception:
        return False


def expand_with_entities(query: str, limit: int = 5) -> list[str]:
    """1-hop entity expansion: find entities mentioned in query, return related entity names."""
    query_lower = query.lower()
    if len(query_lower) < 3:
        return []

    cache_key = f"{query_lower}:{limit}"
    now = _time.time()
    with _expand_cache_lock:
        cached = _expand_cache.get(cache_key)
        if cached and (now - cached[0]) < _EXPAND_CACHE_TTL:
            return list(cached[1])

    # Compute outside lock (Neo4j/SQLite call is slow)
    if _use_neo4j():
        try:
            result = _neo4j_expand(query_lower, limit)
        except Exception:
            result = _sqlite_expand(query_lower, limit)
    else:
        result = _sqlite_expand(query_lower, limit)

    with _expand_cache_lock:
        _expand_cache[cache_key] = (now, result)
        _expand_cache.move_to_end(cache_key)
        if len(_expand_cache) > _EXPAND_CACHE_MAX:
            _expand_cache.popitem(last=False)  # O(1) evict oldest

    return list(result)


def track_access(memory_ids: list[str]) -> None:
    """Bump access count for retrieved memories (memory lifecycle tracking)."""
    if not memory_ids:
        return None
    now = _now()

    if _use_neo4j():
        try:
            return _neo4j_track_access(memory_ids[:20], now)
        except Exception:
            pass
    _sqlite_track_access(memory_ids[:20], now)


def get_stale_memories(days: int = 90, limit: int = 50) -> list[dict]:
    """Find memories never accessed in N days (candidates for review/archival)."""
    if _use_neo4j():
        try:
            return _neo4j_stale_memories(days, limit)
        except Exception:
            pass
    return _sqlite_stale_memories(days, limit)


def get_graph_stats() -> dict:
    """Return entity, relation, and access tracking counts."""
    if _use_neo4j():
        try:
            from neo4j_client import get_stats

            stats = get_stats()
            stats["backend"] = "neo4j"
            return stats
        except Exception:
            pass
    stats = _sqlite_graph_stats()
    stats["backend"] = "sqlite"
    return stats


# ---------------------------------------------------------------------------
# Neo4j implementations
# ---------------------------------------------------------------------------


def _neo4j_store_entities(entities: list, relations: list, now: str, memory_id: str) -> int:
    from neo4j_client import run_query, run_write

    created = 0
    entity_names: dict[str, str] = {}
    # MR1 fix (2026-04-14): map raw LLM-proposed name → canonical name used
    # in entity_names. Relations from the LLM reference the RAW name but
    # after alias resolution the entity_names dict is keyed on the canonical.
    # Without this map, relations whose source/target got aliased were
    # silently dropped. Now relations look up via raw_to_canonical to find
    # the correct entity_names key.
    raw_to_canonical: dict[str, str] = {}

    for ent in entities[:5]:
        raw_name = (ent.get("name") or "").strip().lower()
        etype = ent.get("type", "concept")
        if not raw_name or len(raw_name) < 2 or len(raw_name) > 50:
            continue  # skip empty, too short, or full-sentence names
        # Check if name is an alias of an existing entity
        canonical = resolve_entity(raw_name)
        if canonical and canonical != raw_name:
            # Name is a known alias — merge into existing entity instead
            name = canonical
        else:
            name = raw_name
        raw_to_canonical[raw_name] = name
        eid = f"ent_{uuid.uuid4().hex[:12]}"
        # Classify memory class based on entity type (MemOS pattern)
        # permanent: infra facts, services, people — never decay
        # seasonal: projects, decisions — slow decay
        # ephemeral: concepts, tasks — fast decay
        mem_class = (
            "permanent"
            if etype in ("service", "person", "tool", "agent")
            else ("seasonal" if etype in ("project", "decision") else "ephemeral")
        )
        result = run_query(
            "MERGE (e:Entity {name: $name}) "
            "ON CREATE SET e.id = $id, e.entity_type = $type, e.first_seen_at = $now, "
            "  e.last_seen_at = $now, e.mention_count = 1, e.memory_class = $mem_class "
            "ON MATCH SET e.last_seen_at = $now, e.mention_count = e.mention_count + 1, "
            "  e.memory_class = coalesce(e.memory_class, $mem_class) "
            "RETURN e.id AS id, e.mention_count AS mention_count",
            {"name": name, "id": eid, "type": etype, "now": now, "mem_class": mem_class},
        )
        if result and result[0].get("mention_count", 0) == 1:
            created += 1
        entity_names[name] = result[0]["id"] if result else eid

    for rel in relations[:5]:
        raw_src = (rel.get("source") or "").strip().lower()
        raw_tgt = (rel.get("target") or "").strip().lower()
        rel_type = rel.get("relationship", "related_to")
        # Resolve raw names to their canonical forms for entity_names lookup
        src = raw_to_canonical.get(raw_src, raw_src)
        tgt = raw_to_canonical.get(raw_tgt, raw_tgt)
        if src not in entity_names or tgt not in entity_names or src == tgt:
            continue
        rid = f"rel_{uuid.uuid4().hex[:12]}"
        # Hebbian learning: saturating weight update — neurons that fire together wire together
        run_write(
            # Use separate MATCH clauses to avoid cartesian product warning.
            "MATCH (s:Entity {name: $src}) "
            "MATCH (t:Entity {name: $tgt}) "
            "WHERE s <> t "
            "MERGE (s)-[r:RELATES_TO {relationship: $rel_type}]->(t) "
            "ON CREATE SET r.id = $rid, r.weight = 0.1, r.co_occurrence_count = 1, "
            "  r.confidence = 0.5, r.created_at = $now, r.source_memory_id = $mid, "
            "  r.valid_from = $now, r.valid_to = '' "
            "ON MATCH SET r.co_occurrence_count = coalesce(r.co_occurrence_count, 0) + 1, "
            "  r.weight = CASE WHEN coalesce(r.weight, 0.1) + (0.1 * (1.0 - coalesce(r.weight, 0.1))) > 1.0 "
            "    THEN 1.0 ELSE coalesce(r.weight, 0.1) + (0.1 * (1.0 - coalesce(r.weight, 0.1))) END, "
            "  r.last_confirmed_at = $now",
            {"src": src, "tgt": tgt, "rid": rid, "rel_type": rel_type, "now": now, "mid": memory_id},
        )

    # Phase B (2026-04-14): MENTIONS edges from the memory's MemoryAccess
    # node to every extracted Entity. Enables memory-level spreading
    # activation: "show me memories that mention entity X" becomes a
    # single graph traversal. Previously MemoryAccess was structurally
    # isolated — a stats-only node with no edges.
    if memory_id and entity_names:
        for ent_name in entity_names:
            try:
                run_write(
                    "MERGE (m:MemoryAccess {memory_id: $mid}) "
                    "  ON CREATE SET m.utility_score = 0.5, m.access_count = 0, "
                    "    m.first_accessed_at = $now, m.last_accessed_at = $now, "
                    "    m.memory_class = 'unknown' "
                    "MERGE (e:Entity {name: $name}) "
                    "MERGE (m)-[r:MENTIONS]->(e) "
                    "  ON CREATE SET r.created_at = $now, r.confidence = 0.8",
                    {"mid": memory_id, "name": ent_name, "now": now},
                )
            except Exception:
                # Best-effort — never break the main store path on an
                # edge-write hiccup.
                pass

    log.info("neo4j: stored %d entities, %d relations", len(entity_names), len(relations[:5]))
    return created


def _neo4j_expand(query_lower: str, limit: int) -> list[str]:
    """Spreading activation: weighted multi-hop expansion (biological neural pathway model).
    Activation decays with distance, weighted by relationship strength."""
    from neo4j_client import run_query

    results = run_query(
        "MATCH (seed:Entity) WHERE toLower($q) CONTAINS toLower(seed.name) "
        "WITH seed "
        # 1-hop: activation * edge_weight * 0.7 decay
        "OPTIONAL MATCH (seed)-[r1:RELATES_TO]-(hop1:Entity) "
        "WHERE NOT (toLower($q) CONTAINS toLower(hop1.name)) "
        "WITH seed, hop1, coalesce(r1.weight, 0.5) * 0.7 AS hop1_act "
        # 2-hop: hop1_act * edge_weight * 0.5 decay
        "OPTIONAL MATCH (hop1)-[r2:RELATES_TO]-(hop2:Entity) "
        "WHERE hop2 <> seed AND NOT (toLower($q) CONTAINS toLower(hop2.name)) AND hop2 <> hop1 "
        "WITH collect({name: hop1.name, act: hop1_act}) + "
        "     collect({name: hop2.name, act: hop1_act * coalesce(r2.weight, 0.5) * 0.5}) AS all_nodes "
        "UNWIND all_nodes AS n "
        "WHERE n.name IS NOT NULL "
        "WITH n.name AS name, max(n.act) AS activation "
        "ORDER BY activation DESC "
        "RETURN name LIMIT $limit",
        {"q": query_lower, "limit": limit},
    )
    return [r["name"] for r in results]


def graph_search(query: str, limit: int = 5) -> list[dict]:
    """Search the entity graph directly — returns search-compatible result dicts.

    Matches entities mentioned in the query, returns their properties and
    1-hop relationships as structured content snippets.
    """
    if not _use_neo4j():
        return []
    try:
        from neo4j_client import run_query
    except Exception:
        return []

    query_lower = query.lower()
    if len(query_lower) < 3:
        return []

    try:
        # Find entities mentioned in query + their 1-hop neighbors with relationship info
        results = run_query(
            "MATCH (seed:Entity) WHERE toLower($q) CONTAINS toLower(seed.name) "
            "WITH seed "
            "OPTIONAL MATCH (seed)-[r:RELATES_TO]-(neighbor:Entity) "
            "WITH seed, collect({name: neighbor.name, type: neighbor.entity_type, "
            "  rel: r.relationship, weight: r.weight}) AS neighbors "
            "RETURN seed.name AS name, seed.entity_type AS type, "
            "  seed.mention_count AS mentions, seed.memory_class AS memory_class, "
            "  neighbors "
            "ORDER BY seed.mention_count DESC "
            "LIMIT $limit",
            {"q": query_lower, "limit": limit},
        )
    except Exception:
        return []

    search_results = []
    for r in results:
        name = r.get("name", "")
        etype = r.get("type", "concept")
        mentions = r.get("mentions", 0)
        neighbors = r.get("neighbors", [])
        valid_neighbors = [n for n in neighbors if n.get("name")]

        # Build a content snippet from entity + relationships
        lines = [f"Entity: {name} (type: {etype}, mentions: {mentions})"]
        if valid_neighbors:
            lines.append("Relationships:")
            for n in valid_neighbors[:10]:
                rel = n.get("rel", "related_to")
                w = n.get("weight", 0)
                lines.append(f"  - {rel} → {n['name']} (type: {n.get('type','?')}, weight: {w:.2f})")

        search_results.append(
            {
                "content": "\n".join(lines),
                "path": f"neo4j://entity/{name}",
                "title": f"{name} ({etype})",
                "source": f"neo4j://entity/{name}",
                "source_type": "entity",
                "collection": "graph",
                "score": min(100.0, mentions * 2.0),  # pre-RRF only, overwritten by rrf_score
                "trust_tier": 2,
                "created_at": _now(),  # entities are alive now — avoids time_decay parse errors
                "metadata": {"type": "entity", "entity_type": etype},
            }
        )

    return search_results


def _neo4j_track_access(memory_ids: list[str], now: str) -> None:
    from neo4j_client import run_write

    run_write(
        "UNWIND $ids AS mid "
        "MERGE (m:MemoryAccess {memory_id: mid}) "
        "ON CREATE SET m.access_count = 1, m.first_accessed_at = $now, m.last_accessed_at = $now, "
        "  m.utility_score = 0.5, m.memory_class = 'unknown' "
        "ON MATCH SET m.access_count = m.access_count + 1, m.last_accessed_at = $now",
        {"ids": memory_ids, "now": now},
    )


def reinforce_memory(memory_id: str, success: bool) -> None:
    """MemRL pattern: bump utility_score when a memory proves useful, decrement when not.

    Also updates Qdrant payload trust_score on the semantic_memory record so the
    search ranker (Phase 1E) can use it.
    """
    # Phase 3+4 atoms truth layer + SM-2 spaced repetition.
    # SM-2 quality mapping: success=True → 4, False → 1.
    # Best-effort, no-op if BRAIN_ATOMS_ENABLED is false.
    try:
        from sm2 import apply_quality

        apply_quality(memory_id, quality=4 if success else 1)
    except Exception:
        try:
            from atoms_store import reinforce as atoms_reinforce

            atoms_reinforce(memory_id, success=success)
        except Exception:
            pass

    delta = 0.1 if success else -0.05
    if _use_neo4j():
        try:
            from neo4j_client import run_write

            # v3 bugfix (2026-04-14): was MATCH-only, which silently no-op'd
            # whenever the memory_id wasn't already in MemoryAccess. The bulk
            # of reinforce_memory calls pass chroma_ids like
            # 'semantic_memory:abc123' but the 8631 existing MemoryAccess
            # nodes use file paths from a different ingest path entirely —
            # the two id namespaces never intersected, so utility_score was
            # frozen at 0.5 for everything. MERGE creates-or-updates so
            # every reinforce call now lands whether the node existed or not.
            now_iso = _now()
            run_write(
                "MERGE (m:MemoryAccess {memory_id: $mid}) "
                "ON CREATE SET m.utility_score = 0.5 + $delta, "
                "  m.access_count = 1, m.first_accessed_at = $now, "
                "  m.last_accessed_at = $now, m.memory_class = 'unknown' "
                "ON MATCH SET m.utility_score = CASE "
                "  WHEN coalesce(m.utility_score, 0.5) + $delta > 1.0 THEN 1.0 "
                "  WHEN coalesce(m.utility_score, 0.5) + $delta < 0.0 THEN 0.0 "
                "  ELSE coalesce(m.utility_score, 0.5) + $delta END, "
                "  m.access_count = coalesce(m.access_count, 0) + 1, "
                "  m.last_accessed_at = $now",
                {"mid": memory_id, "delta": delta, "now": now_iso},
            )
        except Exception:
            pass

    # Also update the vector store trust_score payload
    # (fetch current, compute delta, write back via update_payload).
    try:
        from vector_store import get_vector_store

        store = get_vector_store()
        points = store.get(
            "semantic_memory",
            ids=[memory_id],
            with_payload=True,
            with_documents=False,
        )
        if not points:
            return
        try:
            current = float((points[0].payload or {}).get("trust_score", "0.5"))
        except (ValueError, TypeError):
            current = 0.5
        ts_delta = 0.02 if success else -0.05
        new_ts = max(0.0, min(1.0, current + ts_delta))
        store.update_payload(
            "semantic_memory",
            ids=[memory_id],
            patch={"trust_score": round(float(new_ts), 3)},
        )
    except Exception:
        pass


def reinforce_memory_neo4j_only(memory_id: str, success: bool) -> None:
    """Neo4j-only MemRL reinforcement — skips Chroma trust_score and SM-2.

    Exists to close the double-boost bug in memory_lifecycle.reinforce_on_access:
    that function already writes Chroma trust_score directly (so calling full
    reinforce_memory would bump it twice per access). It also shouldn't drive
    SM-2 spaced repetition — retrieval hits are not explicit review events.

    Only touches the Neo4j MemoryAccess.utility_score used by graph ranking.
    Best-effort, no-op when Neo4j is disabled or unreachable.
    """
    if not _use_neo4j():
        return
    delta = 0.1 if success else -0.05
    try:
        from neo4j_client import run_write

        now_iso = _now()
        run_write(
            "MERGE (m:MemoryAccess {memory_id: $mid}) "
            "ON CREATE SET m.utility_score = 0.5 + $delta, "
            "  m.access_count = 1, m.first_accessed_at = $now, "
            "  m.last_accessed_at = $now, m.memory_class = 'unknown' "
            "ON MATCH SET m.utility_score = CASE "
            "  WHEN coalesce(m.utility_score, 0.5) + $delta > 1.0 THEN 1.0 "
            "  WHEN coalesce(m.utility_score, 0.5) + $delta < 0.0 THEN 0.0 "
            "  ELSE coalesce(m.utility_score, 0.5) + $delta END, "
            "  m.access_count = coalesce(m.access_count, 0) + 1, "
            "  m.last_accessed_at = $now",
            {"mid": memory_id, "delta": delta, "now": now_iso},
        )
    except Exception:
        pass


def _neo4j_stale_memories(days: int, limit: int) -> list[dict]:
    from neo4j_client import run_query

    cutoff = datetime.now(UTC)
    from datetime import timedelta

    cutoff_iso = (cutoff - timedelta(days=days)).isoformat(timespec="seconds")
    return run_query(
        "MATCH (m:MemoryAccess) WHERE m.last_accessed_at < $cutoff "
        "RETURN m.memory_id AS memory_id, m.access_count AS access_count, "
        "m.last_accessed_at AS last_accessed_at, m.first_accessed_at AS first_accessed_at "
        "ORDER BY m.access_count ASC LIMIT $limit",
        {"cutoff": cutoff_iso, "limit": limit},
    )


# ---------------------------------------------------------------------------
# SQLite fallback implementations
# ---------------------------------------------------------------------------


def _sqlite_store_entities(entities: list, relations: list, now: str, memory_id: str) -> int:
    conn = _conn()
    try:
        created = 0
        entity_ids: dict[str, str] = {}
        for ent in entities[:5]:
            name = (ent.get("name") or "").strip().lower()
            etype = ent.get("type", "concept")
            if not name or len(name) < 2:
                continue
            existing = conn.execute("SELECT id FROM entities WHERE name = ?", (name,)).fetchone()
            if existing:
                eid = existing["id"]
                conn.execute(
                    "UPDATE entities SET last_seen_at = ?, mention_count = mention_count + 1 WHERE id = ?",
                    (now, eid),
                )
            else:
                eid = f"ent_{uuid.uuid4().hex[:12]}"
                conn.execute(
                    "INSERT INTO entities (id, name, entity_type, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?)",
                    (eid, name, etype, now, now),
                )
                created += 1
            entity_ids[name] = eid
        for rel in relations[:5]:
            src_name = (rel.get("source") or "").strip().lower()
            tgt_name = (rel.get("target") or "").strip().lower()
            relationship = rel.get("relationship", "related_to")
            src_id = entity_ids.get(src_name)
            tgt_id = entity_ids.get(tgt_name)
            if not src_id or not tgt_id or src_id == tgt_id:
                continue
            rid = f"rel_{uuid.uuid4().hex[:12]}"
            conn.execute(
                "INSERT OR IGNORE INTO entity_relations (id, source_entity, relationship, target_entity, confidence, created_at, source_memory_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (rid, src_id, relationship, tgt_id, 0.5, now, memory_id),
            )
        conn.commit()
        return created
    finally:
        conn.close()


def _sqlite_expand(query_lower: str, limit: int) -> list[str]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, name FROM entities WHERE ? LIKE '%' || name || '%'", (query_lower,)
        ).fetchall()
        matched_ids = [row["id"] for row in rows]
        if not matched_ids:
            return []
        related_names = set()
        for eid in matched_ids:
            for r in conn.execute(
                "SELECT e.name FROM entity_relations r JOIN entities e ON r.target_entity = e.id WHERE r.source_entity = ?",
                (eid,),
            ).fetchall():
                related_names.add(r["name"])
            for r in conn.execute(
                "SELECT e.name FROM entity_relations r JOIN entities e ON r.source_entity = e.id WHERE r.target_entity = ?",
                (eid,),
            ).fetchall():
                related_names.add(r["name"])
        related_names = {n for n in related_names if n not in query_lower}
        return list(related_names)[:limit]
    finally:
        conn.close()


def _sqlite_track_access(memory_ids: list[str], now: str) -> None:
    conn = _conn()
    try:
        for mid in memory_ids:
            conn.execute(
                "INSERT INTO memory_access (memory_id, access_count, last_accessed_at, first_accessed_at) VALUES (?, 1, ?, ?) "
                "ON CONFLICT(memory_id) DO UPDATE SET access_count = access_count + 1, last_accessed_at = ?",
                (mid, now, now, now),
            )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def _sqlite_stale_memories(days: int, limit: int) -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT memory_id, access_count, last_accessed_at, first_accessed_at FROM memory_access "
            "WHERE julianday('now') - julianday(last_accessed_at) > ? ORDER BY access_count ASC LIMIT ?",
            (days, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _sqlite_graph_stats() -> dict:
    conn = _conn()
    try:
        entities = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        relations = conn.execute("SELECT COUNT(*) FROM entity_relations").fetchone()[0]
        tracked = conn.execute("SELECT COUNT(*) FROM memory_access").fetchone()[0]
        total = conn.execute("SELECT COALESCE(SUM(access_count), 0) FROM memory_access").fetchone()[0]
        return {
            "entities": entities,
            "relations": relations,
            "tracked_memories": tracked,
            "total_accesses": total,
        }
    finally:
        conn.close()
