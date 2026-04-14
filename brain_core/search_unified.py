#!/opt/homebrew/bin/python3
"""Unified search gateway — fans out to ChromaDB, canonical knowledge, and Obsidian vault.

Usage:
  search_unified.py <query> [-n 5] [--source rag,canonical,obsidian] [--json] [--domain <domain>]

Sources:
  rag       — ChromaDB collections (knowledge, experience, context, semantic_memory)
  canonical — Canonical + distilled notes in ~/server/knowledge/
  obsidian  — Local Obsidian vault mirror

Results are deduplicated and ranked by normalized score with source trust weighting.
"""

import argparse
import json
import re
import subprocess
import sys
import threading
import atexit
from concurrent.futures import ThreadPoolExecutor as _TPE

_search_bg_pool = _TPE(max_workers=2, thread_name_prefix="search_bg")
_search_fanout_pool = _TPE(max_workers=6, thread_name_prefix="search_fanout")
# Shared single-worker pool for Neo4j timeout-wrapped calls in _graph_entity_boost.
# Previously each call constructed + destroyed its own ThreadPoolExecutor — ~1–3ms
# per recall of pool churn. The shared pool is reused across calls.
_graph_boost_pool = _TPE(max_workers=2, thread_name_prefix="graph_boost")
_cooccurrence_lock = threading.Lock()
_cooccurrence_counter = [0]  # mutable container for thread-safe increment
atexit.register(_search_bg_pool.shutdown, wait=False)  # Neo4j writes are best-effort
atexit.register(_search_fanout_pool.shutdown, wait=False)
atexit.register(_graph_boost_pool.shutdown, wait=False)
import time
from pathlib import Path
from datetime import datetime, timezone

# Local module — same dir
sys.path.insert(0, str(Path(__file__).parent))
import temporal  # noqa: E402

# In-process search module imports (replaces subprocess calls in search_rag/search_canonical).
# Failures fall back to subprocess so the existing CLI path keeps working.
try:
    import search as _rag_search  # noqa: E402

    _RAG_IN_PROCESS = True
except Exception:
    _RAG_IN_PROCESS = False

try:
    from config import BRAIN_DIR, OBSIDIAN_VAULT, ONTOLOGY_GRAPH

    _PIPELINE_DIR = BRAIN_DIR / "pipeline"
    KNOWLEDGE_SEARCH = _PIPELINE_DIR / "search_memory.py"
    RAG_SEARCH = BRAIN_DIR / "brain_core" / "search.py"
except ImportError:
    _PIPELINE_DIR = Path("/Users/chrischo/server/brain/pipeline")
    OBSIDIAN_VAULT = Path("/Users/chrischo/.openclaw/workspace/obsidian-vault")
    KNOWLEDGE_SEARCH = Path("/Users/chrischo/server/brain/pipeline/search_memory.py")
    RAG_SEARCH = Path("/Users/chrischo/server/brain/brain_core/search.py")
    ONTOLOGY_GRAPH = Path("/Users/chrischo/.openclaw/memory/ontology/graph.jsonl")

# search_memory is a sibling under brain/pipeline/.
if str(_PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_DIR))
try:
    import search_memory as _canonical_search  # noqa: E402

    _CANONICAL_IN_PROCESS = True
except Exception:
    _CANONICAL_IN_PROCESS = False

SOURCE_TRUST = {
    "canonical": 1.0,
    "distilled": 0.9,
    "knowledge": 0.9,
    "personal": 0.85,  # notes, calendar, tasks, messages (unified)
    "experience": 0.85,
    "semantic_memory": 0.8,
    "context": 0.75,
    "graph": 0.5,
    "obsidian": 0.6,
}

try:
    from tokenizer import tokenize
except ImportError:
    import re as _re

    _TOKEN_RE = _re.compile(r"[a-z0-9_\-]{2,}")

    def tokenize(text):
        return set(_TOKEN_RE.findall((text or "").lower()))


_ontology_cache = None
_ontology_cache_ts = 0.0
_ONTOLOGY_TTL = 300.0  # 5 minutes
_ontology_lock = threading.Lock()


def load_ontology():
    global _ontology_cache, _ontology_cache_ts
    now = time.time()
    with _ontology_lock:
        if _ontology_cache is not None and (now - _ontology_cache_ts) < _ONTOLOGY_TTL:
            return _ontology_cache
        if not ONTOLOGY_GRAPH.exists():
            _ontology_cache = ({}, {})
            _ontology_cache_ts = now
            return _ontology_cache
        entities = {}
        relations = []
        for line in ONTOLOGY_GRAPH.read_text().splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("op") == "create" and "entity" in record:
                ent = record["entity"]
                entities[ent["properties"]["name"].lower()] = ent
            elif record.get("op") == "relate":
                relations.append((record["from"], record["to"]))
        adjacency = {}
        id_to_name = {ent["id"]: ent["properties"]["name"] for ent in entities.values()}
        for frm, to in relations:
            adjacency.setdefault(frm, []).append(to)
            adjacency.setdefault(to, []).append(frm)
        result = (
            entities,
            {id_to_name.get(k, k): [id_to_name.get(v, v) for v in vs] for k, vs in adjacency.items()},
        )
        _ontology_cache = result
        _ontology_cache_ts = now
        return result


def expand_with_ontology(query, adjacency):
    query_lower = query.lower()
    expansions = []
    for name, related in adjacency.items():
        if name.lower() in query_lower:
            expansions.extend(related[:3])
    # Also expand via entity graph (Zep/Graphiti pattern)
    try:
        from entity_graph import expand_with_entities

        entity_expansions = expand_with_entities(query)
        expansions.extend(entity_expansions)
    except Exception:
        pass
    if expansions:
        unique = list(dict.fromkeys(expansions))[:5]
        return query + " " + " ".join(unique)
    return query


_ALL_COLLECTIONS = [
    "knowledge",
    "experience",
    "context",
    "semantic_memory",
    "obsidian",
    "canonical",
    "personal",
]


def search_rag(query, limit, where=None, collections=None):
    """Run hybrid ChromaDB search. Prefers in-process (no Python cold start)
    when the search module imported successfully; falls back to subprocess
    otherwise so the CLI path stays portable.
    """
    if collections is None:
        # Always search all collections — intent routing adjusts trust weights,
        # not collection selection, to avoid missing cross-domain results.
        cols = _ALL_COLLECTIONS
    else:
        cols = list(collections)

    if _RAG_IN_PROCESS:
        try:
            return _rag_search.hybrid_search(
                query, cols, limit, use_keyword=True, where=where, deduplicate=False
            )
        except Exception:
            return []

    # Fallback: subprocess (legacy path). Use the running Python (sys.executable)
    # so dependency resolution matches the parent — brain_server's venv Python.
    collection_arg = ",".join(cols)
    cmd = [sys.executable, str(RAG_SEARCH), query, "-c", collection_arg, "-n", str(limit), "--json"]
    if where:
        cmd.extend(["--where", json.dumps(where)])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0 or not result.stdout.strip():
            return []
        return json.loads(result.stdout)
    except Exception:
        return []


def search_canonical(query, limit, domain=None):
    """Search canonical + distilled notes. In-process when search_memory imported,
    falls back to subprocess otherwise.
    """
    if _CANONICAL_IN_PROCESS:
        try:
            note_hits = _canonical_search.search_notes(query, limit, filter_domain=domain)
            results = [
                _canonical_search.build_note_hit(score, path, metadata, body)
                for score, path, metadata, body in note_hits
            ]
            return results[:limit]
        except Exception:
            return []

    # Fallback: subprocess (legacy path). Use sys.executable to match parent venv.
    cmd = [
        sys.executable,
        str(KNOWLEDGE_SEARCH),
        query,
        "--limit",
        str(limit),
        "--include-rag",
        "--rag-limit",
        "0",
        "--json",
    ]
    if domain:
        cmd.extend(["--domain", domain])
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, cwd=str(KNOWLEDGE_SEARCH.parent)
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        payload = json.loads(result.stdout)
        return payload.get("results", [])
    except Exception:
        return []


def search_obsidian(query, limit):
    """Search Obsidian content via the ChromaDB 'obsidian' collection.

    Previously did a full rglob("*.md") disk scan — O(n) per query, unbounded
    at scale. The obsidian collection is already populated by the scheduled
    reindex job, so searching through ChromaDB is both faster and consistent
    with how every other collection is searched.
    """
    return search_rag(query, limit, collections=["obsidian"])


def normalize_rag_result(r):
    collection = r.get("collection", "knowledge")
    is_canonical = collection == "canonical"
    trust = 1.0 if is_canonical else SOURCE_TRUST.get(collection, 0.7)
    tier = 3 if is_canonical else (2 if r.get("type", "") == "distilled-note" else 1)
    return {
        "id": r.get("id", ""),  # ChromaDB doc id — needed for reinforce-on-access (R10 C1)
        "score": round(r.get("score", 0) * 100 * trust, 2),
        "source_type": "rag",
        "collection": collection,
        "title": r.get("section", "") or r.get("source", "").replace("/Users/chrischo/", "~/"),
        "content": r.get("content", "")[:800],
        "path": r.get("source", ""),
        "trust_tier": tier,
        "created_at": r.get("created_at", ""),
        "metadata": {
            "agent": r.get("agent", ""),
            "service": r.get("service", ""),
            "type": r.get("type", ""),
            "vector_score": r.get("vector_score", 0),
            "keyword_score": r.get("keyword_score", 0),
        },
    }


def normalize_canonical_result(r, query=""):
    source_type = r.get("source_type", "canonical")
    trust = SOURCE_TRUST.get(source_type, 0.9)
    raw_score = r.get("rank_score", 0)
    normalized = min(raw_score, 200) / 200 * 100

    # Penalize canonical results that don't share tokens with the query.
    # Without this, recent high-confidence canonical notes dominate every search
    # regardless of relevance.
    if query:
        query_tokens = tokenize(query)
        title_tokens = tokenize(r.get("title", ""))
        summary_tokens = tokenize(r.get("summary", "")[:400])
        haystack = title_tokens | summary_tokens
        if query_tokens:
            overlap_ratio = len(query_tokens & haystack) / len(query_tokens)
            title_overlap = len(query_tokens & title_tokens) / len(query_tokens)
            # Title match is the strongest signal — canonical wins only if title is relevant
            if title_overlap == 0 and overlap_ratio < 0.5:
                normalized *= 0.2
            elif overlap_ratio < 0.3:
                normalized *= 0.5

    return {
        "score": round(normalized * trust, 2),
        "source_type": source_type,
        "collection": source_type,
        "title": r.get("title", ""),
        "content": r.get("summary", "")[:400],
        "path": r.get("path", ""),
        "trust_tier": 3 if source_type == "canonical" else 2,
        "metadata": {
            "id": r.get("id"),
            "domain": r.get("metadata", {}).get("domain"),
            "confidence": r.get("metadata", {}).get("confidence"),
            "review_state": r.get("metadata", {}).get("review_state"),
        },
    }


def normalize_obsidian_result(r):
    trust = SOURCE_TRUST["obsidian"]
    return {
        "score": round(r.get("score", 0) * 100 * trust, 2),
        "source_type": "obsidian",
        "collection": "obsidian",
        "title": r.get("title", ""),
        "content": r.get("content", "")[:800],
        "path": r.get("path", ""),
        "trust_tier": 0,
        "metadata": {},
    }


def deduplicate(results, max_jaccard_window: int = 80):
    """Two-pass dedup: fast content-hash first, then Jaccard for near-dupes.

    The Jaccard pass is capped at `max_jaccard_window` entries to keep it O(n*k)
    instead of O(n^2) for large result sets.
    """
    import hashlib as _hl

    unique = []
    seen_hashes: set[str] = set()
    seen_tokens: list[set[str]] = []

    for r in results:
        content = r.get("content", "")
        content_hash = _hl.md5(content[:200].encode()).hexdigest()
        if content_hash in seen_hashes:
            continue

        content_toks = tokenize(content)
        is_dup = False
        if content_toks:
            # Only compare against the most recent `max_jaccard_window` entries.
            window = (
                seen_tokens[-max_jaccard_window:] if len(seen_tokens) > max_jaccard_window else seen_tokens
            )
            for prev_toks in window:
                if not prev_toks:
                    continue
                # Cardinality pre-check: skip expensive set intersection when sizes differ by >2x
                min_len = min(len(content_toks), len(prev_toks))
                max_len = max(len(content_toks), len(prev_toks))
                if max_len > 0 and min_len / max_len < 0.5:
                    continue
                overlap = len(content_toks & prev_toks) / max(len(content_toks | prev_toks), 1)
                if overlap > 0.8:
                    is_dup = True
                    break
        if not is_dup:
            unique.append(r)
            seen_hashes.add(content_hash)
            seen_tokens.append(content_toks)
    return unique


_GRAPH_BOOST_FACTOR = 1.15
_GRAPH_BOOST_TIMEOUT_S = 0.05  # 50ms — skip if Neo4j is slow


def _graph_entity_boost(query: str, results: list[dict]) -> set[str]:
    """Boost results connected to entities mentioned in the query via Neo4j graph.

    1. Tokenize query, find matching Entity nodes (case-insensitive).
    2. Walk 2 hops from matched entities to collect source_memory_ids on edges.
    3. Multiply score by _GRAPH_BOOST_FACTOR for results whose id matches.

    Returns set of boosted result IDs (empty if no entities matched or Neo4j down).
    """
    if not query or not results:
        return set()
    try:
        from entity_graph import _use_neo4j

        if not _use_neo4j():
            return set()
        from neo4j_client import run_query as _rq
    except Exception:
        return set()

    # Step 1: find entities mentioned in the query.
    # Use a single Cypher call that checks each entity name against the query.
    query_lower = query.lower()
    try:
        fut = _graph_boost_pool.submit(
            _rq,
            "MATCH (e:Entity) "
            "WHERE size(e.name) >= 2 AND toLower($q) CONTAINS toLower(e.name) "
            "RETURN e.name AS name LIMIT 10",
            {"q": query_lower},
        )
        matched = fut.result(timeout=_GRAPH_BOOST_TIMEOUT_S)
    except Exception:
        return set()

    if not matched:
        return set()

    entity_names = [m["name"] for m in matched]

    # Step 2: walk 2 hops from matched entities, collect source_memory_ids from edges.
    try:
        fut = _graph_boost_pool.submit(
            _rq,
            "MATCH (seed:Entity) WHERE seed.name IN $names "
            "MATCH (seed)-[r1:RELATES_TO]-(hop1) "
            "OPTIONAL MATCH (hop1)-[r2:RELATES_TO]-(hop2) WHERE hop2 <> seed "
            "WITH collect(DISTINCT r1.source_memory_id) + collect(DISTINCT r2.source_memory_id) AS mids "
            "UNWIND mids AS mid "
            "WHERE mid IS NOT NULL AND mid <> '' "
            "RETURN collect(DISTINCT mid) AS memory_ids",
            {"names": entity_names},
        )
        rows = fut.result(timeout=_GRAPH_BOOST_TIMEOUT_S)
    except Exception:
        return set()

    if not rows or not rows[0].get("memory_ids"):
        return set()

    connected_ids = set(rows[0]["memory_ids"])

    # Step 3: boost matching results.
    boosted = set()
    for r in results:
        rid = r.get("id") or ""
        if rid and rid in connected_ids:
            r["score"] = r.get("score", 0) * _GRAPH_BOOST_FACTOR
            r.setdefault("metadata", {})["graph_boost"] = True
            r.setdefault("_debug", {})["graph_boost"] = True
            r.setdefault("_debug", {})["graph_entities"] = entity_names
            boosted.add(rid)
    return boosted


_RELATIONAL_PATTERNS = re.compile(
    r"(?:depends?\s+on|who\s+(?:uses?|owns?|runs?)|which\s+service|what\s+(?:service|depends)|runs?\s+on|connects?\s+to)",
    re.I,
)
_TEMPORAL_PATTERNS = re.compile(
    r"(?:when\s+did|last\s+(?:week|month|year)|yesterday|this\s+(?:week|month)|days?\s+ago|\bhow\s+recent)",
    re.I,
)
_PREFERENCE_PATTERNS = re.compile(
    r"(?:(?:does|what)\s+(?:chris\s+)?prefer|convention|coding\s+standard|(?:chris|he)\s+(?:likes?|always|never))",
    re.I,
)
_CAPITALIZED_WORD = re.compile(r"\b[A-Z][a-zA-Z0-9_-]{1,}\b")


def _is_entity_query(query: str) -> bool:
    """Detect if a query looks like an entity/relational lookup — triggers graph prefetch."""
    if not query:
        return False
    if _RELATIONAL_PATTERNS.search(query):
        return True
    # 2+ capitalized tokens suggest entity mentions (e.g. "OpenClaw Jenna", "Liz Neo4j")
    caps = _CAPITALIZED_WORD.findall(query)
    return len(caps) >= 2


def _prefetch_graph_neighbors(query: str, limit: int = 5) -> list[dict]:
    """Prefetch 2-hop Neo4j neighborhood for an entity query and return boost candidates.

    Expands the query via entity_graph.expand_with_entities() (which already walks
    the Neo4j graph 1-2 hops via RELATES_TO edges), then runs a cheap hybrid search
    in semantic_memory+canonical for each related entity name. Returns normalized
    results that the caller merges into the main RRF fan-out as an additional source.
    """
    try:
        from entity_graph import expand_with_entities

        neighbors = expand_with_entities(query, limit=limit)
    except Exception:
        return []
    if not neighbors:
        return []

    boosted: list[dict] = []
    for name in neighbors[:limit]:
        if not name or len(name) < 2:
            continue
        try:
            raw = search_rag(name, 2, collections=["semantic_memory", "canonical"])
        except Exception:
            continue
        for r in raw:
            if not isinstance(r, dict):
                continue
            normalized = normalize_rag_result(r)
            normalized.setdefault("metadata", {})["graph_prefetch"] = name
            boosted.append(normalized)
    return boosted


_provenance_cache: dict[str, list[str]] = {}
_provenance_lock = threading.Lock()
_PROVENANCE_CACHE_MAX = 500


def _extract_frontmatter_sources(path: str) -> list[str]:
    """Extract sources list from canonical/distilled note frontmatter. Cached."""
    with _provenance_lock:
        if path in _provenance_cache:
            return _provenance_cache[path]
    sources = []
    try:
        p = Path(path)
        if p.exists():
            text = p.read_text(errors="replace")
            if text.startswith("---json"):
                end = text.find("---", 7)
                if end > 0:
                    meta = json.loads(text[7:end])
                    sources = meta.get("sources", [])
    except Exception:
        pass
    with _provenance_lock:
        if len(_provenance_cache) >= _PROVENANCE_CACHE_MAX:
            to_remove = list(_provenance_cache.keys())[:50]
            for k in to_remove:
                del _provenance_cache[k]
        _provenance_cache[path] = sources
    return sources


def _classify_intent(query: str) -> dict[str, float]:
    """Cheap regex intent classifier — returns trust weight multipliers per source.
    Uses phrase-level patterns to avoid false positives on common single words.

    Phase D experiment (2026-04-13): temporal source boost (personal/experience)
    was net-negative on extended track (-0.5pt to -1.0pt). Reverted. The
    temporal_router module remains for future use cases (timetravel endpoints,
    NL date parsing in agent dispatch). Closing the 27.5pt extended gap will
    require summarizing raw_events content into a tier the ranker can match
    against semantic expected_content — that's a Phase 7B+ data plane change,
    not a search routing change.
    """
    if _RELATIONAL_PATTERNS.search(query):
        return {"graph": 1.3, "rag": 0.9, "canonical": 0.9}
    if _TEMPORAL_PATTERNS.search(query):
        return {"graph": 0.7, "rag": 1.0, "canonical": 1.1}
    if _PREFERENCE_PATTERNS.search(query):
        return {"graph": 0.6, "rag": 0.9, "canonical": 1.1}
    return {}


# Phase C2: Source routing — returns subset of sources based on query intent.
_CODE_PATTERNS = re.compile(
    r"\b(how does|how to|function|class|method|api|endpoint|module|import|syntax|error|debug)\b",
    re.IGNORECASE,
)


def _route_sources(query: str, default_sources: list[str]) -> list[str]:
    """Cut latency by skipping sources that won't contribute for this query type.

    Temporal queries → skip graph (mention times aren't in edge weights)
    Preference queries → skip obsidian (notes rarely encode preferences)
    Code/how-to queries → skip canonical + obsidian (look in knowledge/experience)
    Relational queries → use all sources (need graph)
    Default → return full default_sources
    """
    if not query or not default_sources:
        return default_sources
    if _TEMPORAL_PATTERNS.search(query):
        return [s for s in default_sources if s != "graph"]
    if _PREFERENCE_PATTERNS.search(query):
        return [s for s in default_sources if s != "obsidian"]
    if _CODE_PATTERNS.search(query):
        return [s for s in default_sources if s not in ("canonical", "obsidian")]
    return default_sources


def _dedup_by_content_hash(results: list[dict]) -> list[dict]:
    """Phase C4: Dedupe results by content hash BEFORE rerank.

    When multiple sources return the same document (canonical note also in rag),
    keep only the highest-scored version. Saves rerank compute and improves diversity.
    """
    if not results:
        return results
    import hashlib

    seen: dict[str, dict] = {}
    for r in results:
        if not isinstance(r, dict):
            continue
        content = (r.get("content") or "")[:500]
        title = r.get("title") or ""
        key = hashlib.md5(f"{title}|{content}".encode()).hexdigest()[:16]
        existing = seen.get(key)
        if existing is None or r.get("score", 0) > existing.get("score", 0):
            seen[key] = r
    return list(seen.values())


def search_all(
    query,
    limit=5,
    sources=None,
    domain=None,
    original_query=None,
    where=None,
    collections=None,
    entity=None,
    explain=False,
    source_type=None,
    include_history=False,
    include_obsolete=False,
    as_of=None,
    session_id=None,
):
    """Unified search across all sources.

    Phase 1B/1C/1D filters (applied to semantic_memory by default):
      include_history=False — hide memories where superseded_by != ""
      include_obsolete=False — hide memories where memory_class == "obsolete"
      as_of=YYYY-MM-DD — filter to memories valid at that date
    """
    if sources is None:
        sources = ["rag", "canonical", "obsidian"]

    # Phase C2: Intent-based source routing — skip sources that won't help
    # for this query type (temporal, preference, code, relational).
    sources = _route_sources(original_query or query, sources)

    # Bilingual query expansion (free, no LLM) — helps Korean queries find English docs
    try:
        if _RAG_IN_PROCESS:
            variants = _rag_search.expand_query(query)
            if variants and len(variants) > 1:
                query = " ".join(variants)
    except Exception:
        pass

    relevance_query = original_query or query

    rag_results = []
    canonical_results = []
    obsidian_results = []
    graph_results = []
    fts_results = []
    graph_prefetch_results = []

    from concurrent.futures import as_completed

    source_timing: dict[str, int] = {}
    _entity_query = _is_entity_query(relevance_query)

    def _search_rag():
        if "rag" not in sources:
            return []
        t0 = time.time()
        local_where = dict(where) if where else {}
        if source_type:
            type_clause = {"type": {"$eq": source_type}}
            if local_where:
                # Compose both conditions so an existing "type" key in `where`
                # isn't silently overwritten by the source_type filter.
                local_where = {"$and": [local_where, type_clause]}
            else:
                local_where = type_clause
        else:
            # Default exclusion: raw agent/session dumps are ingested for history
            # but flood the vector space with query-like language that crowds out
            # canonical answers. Exclude them by default; callers who want them
            # can pass source_type explicitly.
            _raw_exclude = {
                "type": {
                    "$nin": [
                        "raw-openclaw_session",
                        "raw-claude_code_session",
                        "raw-browser",
                        "raw-git_activity",
                        "raw-screen_time",
                    ]
                }
            }
            if local_where:
                local_where = {"$and": [local_where, _raw_exclude]}
            else:
                local_where = _raw_exclude
        raw_results = search_rag(query, limit * 2, where=local_where or None, collections=collections)

        # Phase 6: atoms-truth-layer batch lookup (BRAIN_ATOMS_READ).
        # Build chroma_id → atoms-row map in ONE SQL prepared statement so the
        # filter loop below stays O(N) without per-row sqlite open/close.
        atoms_meta_map: dict[str, dict] = {}
        try:
            from config import BRAIN_ATOMS_READ

            if BRAIN_ATOMS_READ and raw_results:
                from atoms_store import _conn

                sm_ids = [
                    r["id"]
                    for r in raw_results
                    if isinstance(r, dict) and r.get("collection") == "semantic_memory" and r.get("id")
                ]
                if sm_ids:
                    placeholders = ",".join("?" for _ in sm_ids)
                    with _conn() as _c:
                        rows = _c.execute(
                            f"SELECT chroma_id, tier, superseded_by, valid_from, valid_until "
                            f"FROM atoms WHERE chroma_id IN ({placeholders})",
                            sm_ids,
                        ).fetchall()
                    atoms_meta_map = {row["chroma_id"]: dict(row) for row in rows}
        except Exception:
            atoms_meta_map = {}

        # Phase 1B/1C/1D: filter only applies to semantic_memory collection
        # (other collections don't have supersession/temporal/tier metadata).
        filtered = []
        for r in raw_results:
            if not isinstance(r, dict):
                filtered.append(r)
                continue
            r_coll = r.get("collection", "")
            r_meta = r.get("metadata") or {}
            # Only gate semantic_memory results with lifecycle filters
            if r_coll == "semantic_memory":
                # Phase 6: prefer atoms truth layer for tier/supersession when
                # BRAIN_ATOMS_READ is on and we have a row for this chroma_id.
                atom_row = atoms_meta_map.get(r.get("id") or "")
                tier_meta = (atom_row or {}).get("tier") or r_meta.get("memory_class")
                superseded = (atom_row or {}).get("superseded_by") or r_meta.get("superseded_by")
                vf_meta = ((atom_row or {}).get("valid_from") or r_meta.get("valid_from") or "")[:10]
                vu_meta = ((atom_row or {}).get("valid_until") or r_meta.get("valid_until") or "")[:10]

                if not include_history and superseded:
                    continue
                if not include_obsolete and tier_meta == "obsolete":
                    continue
                # Phase 1C: temporal validity window — compare date portion only
                # to handle ISO timestamps vs date-string as_of cleanly.
                if as_of:
                    as_of_date = as_of[:10]
                    if vf_meta and vf_meta > as_of_date:
                        continue
                    if vu_meta and vu_meta <= as_of_date:
                        continue
            filtered.append(r)
        res = [normalize_rag_result(r) for r in filtered]
        source_timing["rag_ms"] = int((time.time() - t0) * 1000)
        return res

    def _search_canonical():
        if "canonical" not in sources:
            return []
        if collections:
            return []
        t0 = time.time()
        res = [
            normalize_canonical_result(r, query=relevance_query)
            for r in search_canonical(query, limit, domain=domain)
        ]
        source_timing["canonical_ms"] = int((time.time() - t0) * 1000)
        return res

    def _search_obsidian():
        if "obsidian" not in sources:
            return []
        if collections:
            return []
        # Hot-path optimization: _search_rag already iterates _ALL_COLLECTIONS
        # (which includes "obsidian"), so every recall was hitting ChromaDB
        # for the obsidian collection twice — once normalized via
        # normalize_rag_result, once via normalize_obsidian_result — wasting
        # one full hybrid_search fanout (~40–100ms on p95). Suppress the
        # dedicated path when the main rag search covers it; the dedicated
        # fn is still reachable via explicit `sources=["obsidian"]` only.
        if "rag" in sources and "obsidian" in _ALL_COLLECTIONS:
            source_timing["obsidian_ms"] = 0
            return []
        t0 = time.time()
        res = [normalize_obsidian_result(r) for r in search_obsidian(query, limit)]
        source_timing["obsidian_ms"] = int((time.time() - t0) * 1000)
        return res

    def _search_graph():
        if collections:
            return []
        t0 = time.time()
        try:
            from entity_graph import graph_search

            res = graph_search(query, limit=3)
        except Exception:
            res = []
        source_timing["graph_ms"] = int((time.time() - t0) * 1000)
        return res

    def _search_fts():
        t0 = time.time()
        try:
            from fts_index import search_fts

            res = search_fts(query, limit=limit)
        except Exception:
            res = []
        source_timing["fts_ms"] = int((time.time() - t0) * 1000)
        return res

    def _search_graph_prefetch():
        if not _entity_query or collections:
            return []
        t0 = time.time()
        res = _prefetch_graph_neighbors(relevance_query, limit=5)
        source_timing["graph_prefetch_ms"] = int((time.time() - t0) * 1000)
        return res

    # Phase D2 follow-up: temporal_events direct lookup was net-negative
    # (-0.7pt to -1.0pt content_hit on extended). raw_events content is
    # unsummarized and didn't match the eval's semantic expected_content.
    # Reverted to source-boost-only (see _classify_intent personal/experience
    # bumps). The temporal_router module stays for future use cases.

    search_fns = [
        (_search_rag, "rag"),
        (_search_canonical, "canonical"),
        (_search_obsidian, "obsidian"),
        (_search_graph, "graph"),
        (_search_fts, "fts"),
        (_search_graph_prefetch, "graph_prefetch"),
    ]
    result_lists = {
        "rag": rag_results,
        "canonical": canonical_results,
        "obsidian": obsidian_results,
        "graph": graph_results,
        "fts": fts_results,
        "graph_prefetch": graph_prefetch_results,
    }

    future_map = {_search_fanout_pool.submit(fn): name for fn, name in search_fns}
    for fut in as_completed(future_map):
        try:
            result_lists[future_map[fut]].extend(fut.result())
        except Exception:
            pass

    # Entity filter
    if entity:
        ent_lower = entity.lower()

        def matches(r):
            haystack = " ".join(
                [
                    str(r.get("metadata", {}).get("agent", "")),
                    str(r.get("metadata", {}).get("service", "")),
                    str(r.get("path", "")),
                    str(r.get("title", "")),
                    str(r.get("content", ""))[:200],
                ]
            ).lower()
            return ent_lower in haystack

        rag_results[:] = [r for r in rag_results if matches(r)]
        canonical_results[:] = [r for r in canonical_results if matches(r)]
        obsidian_results[:] = [r for r in obsidian_results if matches(r)]
        graph_results[:] = [r for r in graph_results if matches(r)]
        fts_results[:] = [r for r in fts_results if matches(r)]
        graph_prefetch_results[:] = [r for r in graph_prefetch_results if matches(r)]

    # Intent-based trust weight adjustment
    _intent_boost = _classify_intent(relevance_query)

    # RRF fusion across sources with trust-based weights
    try:
        from rrf import rrf_fuse

        source_lists = [
            l
            for l in [
                rag_results,
                canonical_results,
                obsidian_results,
                graph_results,
                fts_results,
                graph_prefetch_results,
            ]
            if l
        ]
        trust_weights = []
        if rag_results:
            trust_weights.append(0.9 * _intent_boost.get("rag", 1.0))
        if canonical_results:
            trust_weights.append(1.0 * _intent_boost.get("canonical", 1.0))
        if obsidian_results:
            trust_weights.append(0.6)
        if graph_results:
            trust_weights.append(0.5 * _intent_boost.get("graph", 1.0))
        if fts_results:
            trust_weights.append(0.4)
        if graph_prefetch_results:
            trust_weights.append(0.7 * _intent_boost.get("graph", 1.0))
        if source_lists:
            all_results = rrf_fuse(source_lists, trust_weights=trust_weights, id_key="path")
        else:
            all_results = []
    except ImportError:
        all_results = (
            rag_results
            + canonical_results
            + obsidian_results
            + graph_results
            + fts_results
            + graph_prefetch_results
        )
        all_results.sort(key=lambda x: (x["score"], x["trust_tier"]), reverse=True)

    unique = deduplicate(all_results)

    # Phase C4: Content-hash dedup BEFORE rerank to save compute + improve diversity
    unique = _dedup_by_content_hash(unique)

    # Round 10 A1 was here originally — moved to AFTER cross-encoder rerank
    # so its boost survives. Rerank replaces r["score"] with rerank_score on
    # line ~730, which would erase the activation boost if we applied it here.

    # M7-WS3: HippoRAG2 query-to-triple linking. Embeds the query and matches
    # against pre-embedded entity-rel-entity triples in Neo4j. Returns a set
    # of entity names that are graph-linked to the query — we use this to
    # boost results whose content mentions any linked entity. Module-level
    # gate via BRAIN_TRIPLE_LINK_ENABLED env var (default off until measured).
    linked_entities: set[str] = set()
    try:
        from triple_link import get_query_linked_entities

        linked_entities = get_query_linked_entities(relevance_query)
    except Exception:
        pass

    # Apply rerank + time_decay. Clamp rerank_score to [0,100] so downstream
    # trust_score and time_decay multipliers stay in a well-defined range —
    # rerank_score is base*relevance*...*boost and can exceed 100, which
    # makes the final score scale undefined.
    try:
        from rerank import rerank as _rerank

        unique = _rerank(relevance_query, unique, top_k=limit * 10)
        for r in unique:
            raw = r.get("rerank_score", r.get("score", 0))
            try:
                raw_f = float(raw)
            except (TypeError, ValueError):
                raw_f = 0.0
            r["score"] = max(0.0, min(100.0, raw_f))
    except ImportError:
        pass

    # M7-WS3: apply linked-entity boost AFTER rerank (mirrors spreading
    # activation pattern below). +5pt bonus per matched entity, capped at
    # +15pt total, so it tiebreaks but never overrides cross-encoder.
    if linked_entities:
        for r in unique:
            content_lower = (r.get("content") or r.get("title") or "").lower()
            matched = sum(1 for e in linked_entities if e and e in content_lower)
            if matched > 0:
                bonus = min(15.0, 5.0 * matched)
                r["score"] = min(100.0, float(r.get("score", 0)) + bonus)
                r["triple_link_matches"] = matched

    # Cross-encoder rerank runs in server.py recall_v2 handler (post-RRF).

    # Round 10 A1 (Wave 1.5b): spreading activation via Personalized PageRank.
    # Now applied AFTER cross-encoder rerank, with two safety conditions:
    # (1) confidence skip — only run when top results are bunched (ambiguous
    #     query); skip when top-1 is a clear winner
    # (2) tiny bonus cap (5pts) so even at max activation, the boost is a
    #     tiebreaker, not a re-rank
    # The activation has its highest value on multi-hop / associative queries
    # where the right answer is reachable via graph neighbors. On single-shot
    # QA where rerank already nails the answer, the conservative cap + skip
    # ensures we don't disturb a correct top-1.
    try:
        from config import BRAIN_SPREADING_ACTIVATION_ENABLED
    except ImportError:
        BRAIN_SPREADING_ACTIVATION_ENABLED = False
    if BRAIN_SPREADING_ACTIVATION_ENABLED and len(unique) >= 2:
        try:
            from spreading_activation import warm_session, boost_results_by_activation

            # Confidence skip: don't perturb a clear top-1.
            # Proportional threshold handles any upstream score range.
            top1 = float(unique[0].get("score", 0))
            top3 = float(unique[min(2, len(unique) - 1)].get("score", 0))
            # Require positive scores for both so negative cross-encoder scores
            # don't silently disable activation. When top1 is weak (<=0) we
            # fall through to apply activation — that's the case that benefits
            # most from entity-graph boosting.
            if top1 > 0 and top3 > 0 and (top3 / top1) >= 0.90:
                activation = warm_session(session_id or "default", relevance_query)
                if activation:
                    boost_results_by_activation(unique, activation, bonus_max=5.0, top_n=limit * 2)
                    source_timing["activation_entities"] = len(activation)
                    source_timing["activation_applied"] = True
                    unique.sort(key=lambda x: x.get("score", 0), reverse=True)
                else:
                    source_timing["activation_applied"] = False
            else:
                source_timing["activation_applied"] = False  # confidence skip
        except Exception:
            pass

    # Phase 1E: trust_score multiplier (feature-flagged, legacy multiplicative path)
    try:
        from config import BRAIN_TRUST_RANKING_ENABLED, BRAIN_SALIENCE_RANKING_ENABLED
    except ImportError:
        BRAIN_TRUST_RANKING_ENABLED = False
        BRAIN_SALIENCE_RANKING_ENABLED = False
    if BRAIN_TRUST_RANKING_ENABLED and not BRAIN_SALIENCE_RANKING_ENABLED:
        for r in unique:
            meta = r.get("metadata") or {}
            try:
                ts = float(meta.get("trust_score", "0.5"))
            except (ValueError, TypeError):
                ts = 0.5
            # trust_score 0.0-1.0 maps to multiplier 0.4-1.0
            r["score"] = r.get("score", 0) * (0.4 + 0.6 * ts)
        unique.sort(key=lambda x: x.get("score", 0), reverse=True)

    # Round 10 A2 (Wave 1.5): Salience as ADDITIVE bonus, not score replacement.
    # Generative Agents' formula adapted to act as a tiebreaker rather than a
    # ranking primary. The rerank_score from cross-encoder is preserved; the
    # salience bonus (capped at +10pts) only matters when results are tied.
    if BRAIN_SALIENCE_RANKING_ENABLED and unique:
        import math as _math
        from datetime import datetime as _dt, timezone as _tz

        SALIENCE_BONUS_MAX = 10.0  # bounded — tiebreaks, doesn't dominate
        RECENCY_HALFLIFE_DAYS = 90.0

        def _recency_from_iso(ts: str) -> float:
            if not ts:
                return 0.0
            try:
                dt = _dt.fromisoformat(ts.rstrip("Zz"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_tz.utc)
                age_days = (_dt.now(_tz.utc) - dt).total_seconds() / 86400
                if age_days < 0:
                    return 1.0
                return _math.exp(-age_days * _math.log(2) / RECENCY_HALFLIFE_DAYS)
            except Exception:
                return 0.0

        for r in unique:
            meta = r.get("metadata") or {}
            try:
                trust = float(meta.get("trust_score", 0.5))
            except (ValueError, TypeError):
                trust = 0.5
            try:
                access = int(meta.get("access_count", 0))
            except (ValueError, TypeError):
                access = 0
            access_norm = _math.log(access + 1) / _math.log(50)
            # Combine trust + access into a single importance signal — was
            # using max() which silently dropped whichever was smaller. The
            # average preserves both signals so a high-trust never-accessed
            # memory and a low-trust frequently-accessed one each surface.
            importance = (min(trust, 1.0) + min(access_norm, 1.0)) / 2.0

            recency = r.get("recency_score")
            if recency is None:
                recency = _recency_from_iso(r.get("created_at", "") or meta.get("created_at", ""))
            recency = float(recency)

            bonus = SALIENCE_BONUS_MAX * (recency + importance) / 2.0
            r["score"] = float(r.get("score", 0)) + bonus
            r["salience_components"] = {
                "recency": round(recency, 3),
                "importance": round(importance, 3),
                "bonus": round(bonus, 2),
            }
        unique.sort(key=lambda x: x.get("score", 0), reverse=True)

    try:
        from time_decay import apply_to_results

        unique = apply_to_results(unique)
        unique.sort(key=lambda x: x.get("score", 0), reverse=True)
    except ImportError:
        pass

    # Preference recency boost: for semantic_memory preferences, newer ones
    # get a significant boost to prevent stale preferences from dominating
    # via accumulated access_count reinforcement.
    now_utc = datetime.now(timezone.utc)
    for r in unique:
        collection = r.get("collection", "")
        if collection != "semantic_memory":
            continue
        meta = r.get("metadata") or {}
        if meta.get("category") != "preference":
            continue
        created_at_raw = meta.get("created_at") or meta.get("updated_at")
        if not created_at_raw:
            continue
        try:
            ts = created_at_raw.replace("Z", "+00:00")
            created_dt = datetime.fromisoformat(ts)
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=timezone.utc)
            age_days = max(0, (now_utc - created_dt).total_seconds() / 86400)
            # 30-day half-life recency curve: a preference from today gets 1.0,
            # from 30 days ago gets 0.5, from 90 days ago gets 0.125
            recency = 0.5 ** (age_days / 30)
            # Blend: 60% original score + 40% recency-weighted score
            # This ensures a fresh preference strongly outranks a stale one
            # while still respecting relevance (can't boost an irrelevant result)
            r["score"] = round(r["score"] * (0.6 + 0.4 * recency), 2)
        except (ValueError, TypeError):
            continue

    # Graph-aware boost: find entities mentioned in query, look up connected
    # memory_ids via Neo4j 2-hop traversal, boost results whose IDs match.
    # Guarded by a 50ms timeout so Neo4j latency never blocks search.
    try:
        graph_boosted_ids = _graph_entity_boost(relevance_query, unique)
        if graph_boosted_ids:
            source_timing["graph_boost_count"] = len(graph_boosted_ids)
            unique.sort(key=lambda x: x.get("score", 0), reverse=True)
    except Exception:
        pass

    # Round 10 B2 (Wave 2): Episodic peer cross-promotion.
    # When a top-N result has peer memories from the same temporal episode
    # (built nightly by pipeline/episode_binder.py), boost those peers so the
    # whole "moment" comes back together. CoALA-style episodic memory binding.
    #
    # Two safety conditions copied from the Wave 1.5 fixes:
    #   1. Confidence skip — don't perturb a clear top-1 result
    #   2. Tiny bonus cap (2pts) so peers can only displace top-N members
    #      that are already neck-and-neck with them. Plus the boost is
    #      capped at min(top_N_score - 1) so it never displaces an
    #      authoritative top-N member, only joins the bottom of the pack.
    try:
        from config import BRAIN_EPISODIC_BINDING_ENABLED
    except ImportError:
        BRAIN_EPISODIC_BINDING_ENABLED = False
    if BRAIN_EPISODIC_BINDING_ENABLED and len(unique) >= 3:
        try:
            # Confidence skip: clear top-1 winner stays put.
            # Proportional threshold handles any upstream score range.
            top1 = float(unique[0].get("score", 0))
            top3 = float(unique[min(2, len(unique) - 1)].get("score", 0))
            if top1 > 0 and top3 > 0 and (top3 / top1) >= 0.90:
                import sqlite3 as _sql
                from pathlib import Path as _P

                db_path = _P("/Users/chrischo/server/brain/logs/autonomy.db")
                if db_path.exists():
                    top_n = unique[:limit]
                    top_ids = [
                        r.get("id") or r.get("path", "") for r in top_n if r.get("id") or r.get("path")
                    ]
                    if top_ids:
                        _conn = _sql.connect(str(db_path))
                        try:
                            placeholders = ",".join("?" * len(top_ids))
                            top_episodes = _conn.execute(
                                f"SELECT DISTINCT episode_id FROM episode_membership WHERE memory_id IN ({placeholders})",
                                top_ids,
                            ).fetchall()
                            if top_episodes:
                                ep_ids = [e[0] for e in top_episodes]
                                ep_placeholders = ",".join("?" * len(ep_ids))
                                peer_rows = _conn.execute(
                                    f"SELECT memory_id FROM episode_membership WHERE episode_id IN ({ep_placeholders})",
                                    ep_ids,
                                ).fetchall()
                                peer_set = {pid for (pid,) in peer_rows}
                                top_id_set = set(top_ids)
                                # Floor: peer score after boost must remain
                                # below the lowest top-N score, so peers can
                                # only join the bottom, never displace.
                                lowest_top_n = float(top_n[-1].get("score", 0))
                                EPISODIC_BONUS_MAX = 2.0
                                boosted_count = 0
                                for r in unique:
                                    rid = r.get("id") or r.get("path", "")
                                    if rid in peer_set and rid not in top_id_set:
                                        try:
                                            cur = float(r.get("score", 0))
                                            # Cap so the boost doesn't push us above any top-N member
                                            headroom = max(0.0, lowest_top_n - cur - 0.1)
                                            bonus = min(EPISODIC_BONUS_MAX, headroom)
                                            if bonus > 0:
                                                r["score"] = cur + bonus
                                                r["episode_peer"] = True
                                                boosted_count += 1
                                        except (TypeError, ValueError):
                                            pass
                                if boosted_count > 0:
                                    source_timing["episode_peers_boosted"] = boosted_count
                                    unique.sort(key=lambda x: x.get("score", 0), reverse=True)
                        finally:
                            _conn.close()
        except Exception:
            pass

    # Round 10 A3 (Wave 1.5): MMR with confidence-aware skip.
    # Carbonell & Goldstein '98 — diversify only when results are genuinely
    # ambiguous. If top-1 dominates the top-N spread, returning the top-N
    # by relevance is the correct answer; diversification just adds noise.
    try:
        from config import BRAIN_MMR_DIVERSITY_ENABLED, BRAIN_MMR_LAMBDA
    except ImportError:
        BRAIN_MMR_DIVERSITY_ENABLED = False
        BRAIN_MMR_LAMBDA = 0.85
    # Confidence skip — when top scores are well-separated, MMR can only hurt.
    # Use a proportional threshold so it works regardless of whether upstream
    # reranking produced RRF-style scores (~100 range), cross-encoder sigmoid
    # outputs (~0-10 range), or token overlap ratios (~0-1 range).
    _mmr_should_run = BRAIN_MMR_DIVERSITY_ENABLED and len(unique) > limit
    if _mmr_should_run:
        _top_score = float(unique[0].get("score", 0))
        _nth_score = float(unique[min(limit - 1, len(unique) - 1)].get("score", 0))
        # Run MMR only if the nth result is >= 85% of top-1 (ambiguous spread).
        # Non-positive scores disable the skip entirely — fall through to run
        # MMR, which is the correct behavior when the whole top-N is weak.
        _mmr_should_run = _top_score > 0 and _nth_score > 0 and (_nth_score / _top_score) >= 0.85
    if _mmr_should_run:
        from tokenizer import tokenize as _tok

        # Pre-tokenize once per result to avoid O(n^2) re-tokenization
        token_cache: list[set[str]] = []
        for r in unique:
            text = (r.get("title", "") or "") + " " + (r.get("content", "") or "")[:500]
            token_cache.append(_tok(text))

        def _jacc(i: int, j: int) -> float:
            a, b = token_cache[i], token_cache[j]
            if not a or not b:
                return 0.0
            return len(a & b) / max(len(a | b), 1)

        max_score = max((float(r.get("score", 0)) for r in unique), default=1.0) or 1.0
        # Always seed with the highest-scoring result
        selected_idx: list[int] = [0]
        remaining_idx: list[int] = list(range(1, len(unique)))
        while remaining_idx and len(selected_idx) < limit * 2:
            best_idx, best_score = -1, -1e9
            for i in remaining_idx:
                relevance = float(unique[i].get("score", 0)) / max_score
                max_sim = max((_jacc(i, s) for s in selected_idx), default=0.0)
                mmr_score = BRAIN_MMR_LAMBDA * relevance - (1 - BRAIN_MMR_LAMBDA) * max_sim
                if mmr_score > best_score:
                    best_score = mmr_score
                    best_idx = i
            if best_idx < 0:
                break
            selected_idx.append(best_idx)
            remaining_idx.remove(best_idx)
        # Reorder unique to match MMR selection order
        unique = [unique[i] for i in selected_idx] + [unique[i] for i in remaining_idx]

    # Source diversity: prevent same source file from dominating top-k
    source_counts: dict[str, int] = {}
    diverse = []
    overflow = []
    for r in unique:
        src = r.get("path", "")
        source_counts[src] = source_counts.get(src, 0) + 1
        if source_counts[src] <= 3:
            diverse.append(r)
        else:
            overflow.append(r)
    unique = diverse + overflow

    final_results = unique[:limit]

    # Conflict flagging: mark results that may contradict each other
    for i, r1 in enumerate(final_results):
        for r2 in final_results[i + 1 :]:
            if r1.get("collection") == r2.get("collection"):
                continue
            t1 = tokenize(r1.get("content", "")[:300])
            t2 = tokenize(r2.get("content", "")[:300])
            if t1 and t2:
                overlap = len(t1 & t2) / max(len(t1 | t2), 1)
                if 0.3 < overlap < 0.7:
                    r1.setdefault("flags", []).append("potential_conflict")
                    r2.setdefault("flags", []).append("potential_conflict")

    # Attach provenance metadata — trace result back to source type + upstream IDs
    for r in final_results:
        path = r.get("path", "")
        col = r.get("collection", "")
        provenance = {"collection": col, "sources": []}
        if "/canonical/" in path:
            provenance["tier"] = "canonical"
            provenance["sources"] = _extract_frontmatter_sources(path)
        elif "/distilled/" in path:
            provenance["tier"] = "distilled"
            provenance["sources"] = _extract_frontmatter_sources(path)
        elif col == "semantic_memory":
            provenance["tier"] = "semantic"
        elif col == "knowledge":
            provenance["tier"] = "config"
        elif col == "graph":
            provenance["tier"] = "graph"
        else:
            provenance["tier"] = "raw"
        r["provenance"] = provenance

    # Track memory access for lifecycle management (fire-and-forget, non-blocking)
    try:
        from entity_graph import track_access

        access_ids = [r.get("id") or r.get("path", "") for r in final_results if r.get("id") or r.get("path")]
        if access_ids:
            _search_bg_pool.submit(track_access, access_ids)
    except Exception:
        pass

    # Reinforce entity relationships when entities co-appear in search results (Hebbian).
    # Rate-limited: only every 5th query to reduce Neo4j write traffic.
    with _cooccurrence_lock:
        _cooccurrence_counter[0] += 1
        _should_reinforce = _cooccurrence_counter[0] % 5 == 0
    if _should_reinforce:
        try:

            def _reinforce_cooccurrence():
                from entity_graph import _use_neo4j

                if not _use_neo4j():
                    return
                from neo4j_client import run_query as _rq, run_write as _rw

                result_text = " ".join(
                    (r.get("content", "") + " " + r.get("title", ""))[:200] for r in final_results[:5]
                ).lower()
                if len(result_text) < 50:
                    return
                matched = _rq(
                    "MATCH (e:Entity) WHERE toLower($text) CONTAINS toLower(e.name) RETURN e.name AS name",
                    {"text": result_text},
                )
                names = [m["name"] for m in matched]
                if len(names) < 2:
                    return
                pairs_done = 0
                for i, a in enumerate(names[:5]):
                    for b in names[i + 1 : 5]:
                        if a != b and pairs_done < 3:
                            from datetime import datetime as _dt, timezone as _tz

                            _rw(
                                # Use separate MATCH clauses to avoid cartesian product warning.
                                "MATCH (s:Entity {name: $a}) "
                                "MATCH (t:Entity {name: $b}) "
                                "MERGE (s)-[r:RELATES_TO {relationship: 'co_retrieved'}]->(t) "
                                "ON CREATE SET r.weight = 0.05, r.co_occurrence_count = 1, r.created_at = $now "
                                "ON MATCH SET r.co_occurrence_count = r.co_occurrence_count + 1, "
                                "  r.weight = CASE WHEN r.weight + (0.05 * (1.0 - r.weight)) > 1.0 THEN 1.0 "
                                "  ELSE r.weight + (0.05 * (1.0 - r.weight)) END",
                                {"a": a, "b": b, "now": _dt.now(_tz.utc).isoformat(timespec="seconds")},
                            )
                            pairs_done += 1

            _search_bg_pool.submit(_reinforce_cooccurrence)
        except Exception:
            pass

    payload = {
        "query": query,
        "results": final_results,
        "sources_searched": sources,
        "total_candidates": len(all_results),
        "source_timing": source_timing,
    }
    if explain:
        payload["filter_applied"] = {
            "where": where,
            "entity": entity,
            "collections": collections,
            "domain": domain,
        }

    try:
        import hooks

        hooks.fire(
            "on_search", query=query, result_count=len(final_results), latency_ms=sum(source_timing.values())
        )
    except Exception:
        pass

    return payload


def main():
    parser = argparse.ArgumentParser(description="Unified Search Gateway")
    parser.add_argument("query", help="Search query")
    parser.add_argument("-n", "--limit", type=int, default=5, help="Number of results")
    parser.add_argument(
        "--source",
        default="rag,canonical,obsidian",
        help="Sources to search (comma-separated: rag,canonical,obsidian)",
    )
    parser.add_argument(
        "--domain",
        default=None,
        choices=["chris", "projects", "infra", "decisions", "incidents"],
        help="Filter canonical notes by domain",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="Temporal lower bound (e.g. '2026-04-01', '7d', 'last tuesday', 'yesterday')",
    )
    parser.add_argument("--until", default=None, help="Temporal upper bound (e.g. '2026-04-07', 'today')")
    parser.add_argument(
        "--entity",
        default=None,
        help="Filter to results mentioning this entity (agent, service, path, or content)",
    )
    parser.add_argument(
        "--collection",
        default=None,
        help="Restrict to a specific ChromaDB collection (e.g. messages, notes, calendar, tasks, experience)",
    )
    parser.add_argument(
        "--explain", action="store_true", help="Include applied filters in the result payload"
    )
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    sources = [s.strip() for s in args.source.split(",")]

    # Temporal range is applied Python-side after retrieval because ChromaDB 1.4.1
    # rejects string operands in $gte/$lt and our created_at metadata is ISO strings.
    start_dt, end_dt = temporal.parse_range(args.since, args.until)
    where = None

    collections = [args.collection] if args.collection else None

    _, adjacency = load_ontology()
    expanded_query = expand_with_ontology(args.query, adjacency)

    search_limit = args.limit * 3 if (start_dt or end_dt) else args.limit
    payload = search_all(
        expanded_query,
        search_limit,
        sources,
        args.domain,
        original_query=args.query,
        where=where,
        collections=collections,
        entity=args.entity,
        explain=args.explain,
    )
    if (start_dt or end_dt) and isinstance(payload, dict):
        payload["results"] = temporal.filter_by_created_at(payload.get("results", []), start_dt, end_dt)[
            : args.limit
        ]
    payload["original_query"] = args.query
    if expanded_query != args.query:
        payload["expanded_query"] = expanded_query
    if args.since or args.until:
        payload["temporal_range"] = {
            "since": start_dt.isoformat() if start_dt else None,
            "until": end_dt.isoformat() if end_dt else None,
        }

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    print(f"\nQuery: '{args.query}'")
    if expanded_query != args.query:
        print(f"Expanded: '{expanded_query}'")
    print(f"Sources: {', '.join(sources)} | Candidates: {payload['total_candidates']}")
    print("=" * 60)

    for i, r in enumerate(payload["results"]):
        trust_label = {3: "CANONICAL", 2: "DISTILLED", 1: "RAG", 0: "OBSIDIAN"}.get(r["trust_tier"], "?")
        print(f"\n#{i+1} (score: {r['score']:.1f}) [{trust_label}:{r['collection']}]")
        print(f"  {r['title']}")
        print(f"  {r['content'][:200]}...")


if __name__ == "__main__":
    main()
