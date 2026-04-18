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
import atexit
import json
import re
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor as _TPE

_search_bg_pool = _TPE(max_workers=2, thread_name_prefix="search_bg")
_search_fanout_pool = _TPE(max_workers=6, thread_name_prefix="search_fanout")

# 2026-04-16 R-7: thread-local sqlite connections for autonomy.db.
# Previously every episodic-binding recall + every spreading-activation
# store/load opened a fresh sqlite3.connect() and closed it — cheap
# (~0.5ms) but additive under concurrent load. threading.local avoids
# the connect overhead without cross-thread corruption since sqlite
# connections are not thread-safe across threads.
_autonomy_conn_local = threading.local()


def _get_autonomy_conn():
    """Return a thread-local connection to autonomy.db (WAL mode)."""
    conn = getattr(_autonomy_conn_local, "conn", None)
    if conn is None:
        try:
            autonomy_path = Path("/Users/chrischo/server/brain/logs/autonomy.db")
            if not autonomy_path.exists():
                return None
            conn = sqlite3.connect(str(autonomy_path), check_same_thread=False, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            _autonomy_conn_local.conn = conn
        except Exception:
            return None
    return conn


# Shared single-worker pool for Neo4j timeout-wrapped calls in _graph_entity_boost.
# Previously each call constructed + destroyed its own ThreadPoolExecutor — ~1–3ms
# per recall of pool churn. The shared pool is reused across calls.
_graph_boost_pool = _TPE(max_workers=2, thread_name_prefix="graph_boost")

# 2026-04-17 fix: _search_rag previously created a fresh ThreadPoolExecutor
# per call. shutdown(wait=False) signals but threads didn't always drain
# before the next call created another pool — accumulated under load.
_search_rag_split_pool = _TPE(max_workers=4, thread_name_prefix="rag_split")
_cooccurrence_lock = threading.Lock()
_cooccurrence_counter = [0]  # mutable container for thread-safe increment
atexit.register(_search_bg_pool.shutdown, wait=False)  # Neo4j writes are best-effort
atexit.register(_search_fanout_pool.shutdown, wait=False)
atexit.register(_graph_boost_pool.shutdown, wait=False)
atexit.register(_search_rag_split_pool.shutdown, wait=False)
import time
from datetime import UTC, datetime
from pathlib import Path

# Local module — same dir
sys.path.insert(0, str(Path(__file__).parent))
import temporal  # noqa: E402

# In-process search module imports (replaces subprocess calls in search_rag/search_canonical).
# Failures fall back to subprocess so the existing CLI path keeps working.
try:
    import search as _rag_search

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
    import search_memory as _canonical_search

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
            # M9.2: thread parent-child chunking fields through so the
            # retrieval-side expand pass (parent_child_expand.py) can find
            # children with parent_id and swap in parent content.
            "parent_id": r.get("parent_id") or r.get("metadata", {}).get("parent_id"),
            "is_parent": r.get("is_parent") or r.get("metadata", {}).get("is_parent", False),
            "chunk_id": r.get("chunk_id") or r.get("metadata", {}).get("chunk_id"),
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
        "content": r.get("summary", "")[:2000],
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
            # 2026-04-16 R-4: avoid mutating shared metadata dicts — the
            # RRF copy is shallow so nested dicts like `metadata` are
            # shared across calls. Assign fresh dicts scoped to THIS
            # result so the boost marker can't bleed into other results
            # that happen to share the same metadata source.
            _meta = dict(r.get("metadata") or {})
            _meta["graph_boost"] = True
            r["metadata"] = _meta
            _dbg = dict(r.get("_debug") or {})
            _dbg["graph_boost"] = True
            _dbg["graph_entities"] = entity_names
            r["_debug"] = _dbg
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
# 2026-04-17: concrete infra lookup patterns — query is asking for a literal
# config value (port, proxy host, rate limit, env var) rather than Chris's
# philosophical preference. These should prefer actual config files in the
# knowledge/code collections over canonical preference notes which only
# meta-discuss the topic.
_CONCRETE_INFRA_PATTERNS = re.compile(
    r"\b(?:port|container|reverse\s*prox(?:y|ies)|rate\s+limit|limit_req|"
    r"nginx\s+(?:config|conf|block|rule)|docker\s+compose|server\s+block|"
    r"credentials?\s+(?:file|path|location)|env(?:ironment)?\s+var|"
    r"upstream|listen\s+\d|proxy_pass)\b",
    re.I,
)

# 2026-04-17 Phase 10 modality expansion (7 buckets) — inspired by friend's
# SECONDBRAIN_MODALITY_WEIGHTS pattern. Each bucket shifts trust weights to
# the sources most likely to contain the answer for that query type.
#
# Bucket inventory:
#   1. relational   — "who uses X?", "what depends on Y?" → graph 1.3x
#   2. temporal     — "when did…", "last week", "recent" → canonical 1.1x
#   3. preference   — "Chris prefers", "convention" → canonical 1.1x
#   4. concrete_infra — "nginx port", "rate_req config" → rag 1.25x
#   5. code         — "function X", "import Y", "how does Z work" → rag 1.3x
#   6. agent_role   — "Liz role", "Jenna AGENTS.md" → rag 1.2x, canonical 0.9x
#   7. narrative    — "what happened when X", "why did Y" → canonical 1.15x

_CODE_MODALITY_PATTERNS = re.compile(
    r"\b(?:function|def\s+\w|class\s+[A-Z]|import\s+\w|from\s+\w+\s+import|"
    r"how\s+does\s+\w+\s+(?:work|handle)|"
    r"(?:api|endpoint|method|return\s+value|parameter|argument)\s+\w+|"
    r"async\s+def|await\s+|throw\s+|catch\s+|try\s*\{|\.venv/|__init__\.py)\b",
    re.I,
)

_AGENT_ROLE_PATTERNS = re.compile(
    r"\b(?:jenna|liz|ellie|sage|market)\s+"
    r"(?:role|responsibilit(?:y|ies)|duty|duties|primary|AGENTS?\.md|TOOLS?\.md|"
    r"does|handle|owns?|scope)\b|"
    r"\b(?:role|primary\s+role|domain)\s+of\s+(?:jenna|liz|ellie|sage|market)\b",
    re.I,
)

_NARRATIVE_PATTERNS = re.compile(
    r"\b(?:what\s+happened\s+(?:when|during|after)|"
    r"why\s+did\s+\w+\s+(?:choose|decide|switch|move|archive|pick)|"
    r"the\s+story\s+of|history\s+of|how\s+we\s+(?:got|arrived|ended\s+up))\b",
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


# 2026-04-16 R-4: FIFO→LRU. Previous dict was ordered by insertion so
# the first 50 evicted were always the oldest-inserted, never the
# least-recently-used. Frequently-read canonical paths inserted early
# were evicted every round, causing repeated disk reads. OrderedDict
# with move_to_end on hit gives true LRU semantics.
from collections import OrderedDict

_provenance_cache: OrderedDict[str, list[str]] = OrderedDict()
_provenance_lock = threading.Lock()
_PROVENANCE_CACHE_MAX = 500


def _extract_frontmatter_sources(path: str) -> list[str]:
    """Extract sources list from canonical/distilled note frontmatter. LRU-cached."""
    with _provenance_lock:
        if path in _provenance_cache:
            _provenance_cache.move_to_end(path)
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
            # LRU eviction: drop the oldest-accessed entry
            _provenance_cache.popitem(last=False)
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
    # 2026-04-17 Phase 10 modality: 7-bucket routing. Order matters — more
    # specific patterns first (agent_role before preference; code before
    # concrete_infra) so that a narrow query doesn't get captured by a
    # broader bucket.
    if _RELATIONAL_PATTERNS.search(query):
        return {"graph": 1.3, "rag": 0.9, "canonical": 0.9}
    if _AGENT_ROLE_PATTERNS.search(query):
        # Agent role queries — answer lives in ~/.openclaw/workspace-*/AGENTS.md
        # (indexed under 'knowledge' via rag), not canonical preference atoms.
        return {"graph": 0.9, "rag": 1.2, "canonical": 0.9}
    if _CODE_MODALITY_PATTERNS.search(query):
        # Code questions — prefer `code` + `knowledge` collections over
        # canonical which has prose about design decisions, not syntax.
        return {"graph": 0.8, "rag": 1.3, "canonical": 0.8}
    if _CONCRETE_INFRA_PATTERNS.search(query):
        # Literal config values — boost rag (docker-compose/nginx conf) over
        # canonical preference notes.
        return {"graph": 0.8, "rag": 1.25, "canonical": 0.75}
    if _NARRATIVE_PATTERNS.search(query):
        # "what happened", "why did" — canonical synthesizes narrative arcs.
        return {"graph": 0.9, "rag": 0.95, "canonical": 1.15}
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


# 2026-04-17 search trace observability.
# When enabled, every search_all() run appends one line to the trace
# log capturing: query, source counts, per-source latency, trust
# weights, fusion mode, post-fusion result count, intent_boost.
# Chris uses this to diagnose "why did query X return result Y instead
# of Z" post-hoc — the ranking pipeline has 7 sources × RRF × rerank
# × time-decay × LtR, and with no trace it's a black box.
#
# Always on (capped to avoid runaway log): records last 5000 entries
# via fixed-size ring via rename-on-threshold. Gated off entirely by
# BRAIN_SEARCH_TRACE_DISABLED=1 env for emergency bypass.
_SEARCH_TRACE_PATH = None
_SEARCH_TRACE_MAX_BYTES = 5 * 1024 * 1024  # 5MB before rotation


def _get_search_trace_path() -> Path | None:
    global _SEARCH_TRACE_PATH
    if _SEARCH_TRACE_PATH is not None:
        return _SEARCH_TRACE_PATH
    try:
        from config import BRAIN_LOGS_DIR as _LOGS
    except ImportError:
        _LOGS = Path("/Users/chrischo/server/brain/logs")
    _SEARCH_TRACE_PATH = _LOGS / "search_trace.jsonl"
    return _SEARCH_TRACE_PATH


def _maybe_emit_search_trace(
    *,
    query: str,
    original_query: str | None,
    source_counts: dict,
    source_timing: dict,
    trust_weights: list,
    fusion_mode: str,
    total_after_fusion: int,
    intent_boost: dict,
) -> None:
    import os as _os

    if _os.environ.get("BRAIN_SEARCH_TRACE_DISABLED", "").strip() in ("1", "true", "yes"):
        return
    path = _get_search_trace_path()
    if path is None:
        return
    try:
        # Lightweight rotation: rename to .1 when exceeding max size.
        if path.exists() and path.stat().st_size > _SEARCH_TRACE_MAX_BYTES:
            rotated = path.with_suffix(".jsonl.1")
            try:
                if rotated.exists():
                    rotated.unlink()
                path.rename(rotated)
            except OSError:
                pass
        entry = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "q": (query or "")[:200],
            "q_orig": (original_query or "")[:200] if original_query else None,
            "src_counts": source_counts,
            "src_ms": source_timing,
            "trust_w": [round(w, 3) for w in trust_weights],
            "fusion": fusion_mode,
            "total": total_after_fusion,
            "intent_boost": {k: round(v, 3) for k, v in intent_boost.items()} if intent_boost else None,
        }
        with path.open("a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as _exc:
        log.debug("search trace write failed: %s", _exc)


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
    include_provisional=False,
    include_all_speakers=False,
    include_session_scope=False,
    include_low_trust=False,
    include_expired=False,
):
    """Unified search across all sources.

    Phase 1B/1C/1D filters (applied to semantic_memory by default):
      include_history=False      — hide memories where superseded_by != ""
      include_obsolete=False     — hide memories where memory_class == "obsolete"
      as_of=YYYY-MM-DD           — filter to memories valid at that date

    v3 F6 (2026-04-14) — explicit per-filter escape hatches. Previously
    include_history was overloaded as the single disable-all-hygiene-filters
    flag, which made intent ambiguous. Now each of the 5 hygiene filters has
    its own kwarg so callers say exactly what they want:
      include_provisional=True   — include atoms still tagged provisional
      include_all_speakers=True  — include agent:* / quoted:* speakers
      include_session_scope=True — include session-scoped atoms
      include_low_trust=True     — include atoms with trust_score < 0.3
      include_expired=True       — include atoms whose valid_until is in the past
    """
    if sources is None:
        sources = ["rag", "canonical", "obsidian"]

    # Phase C2: Intent-based source routing — skip sources that won't help
    # for this query type (temporal, preference, code, relational).
    sources = _route_sources(original_query or query, sources)

    # Bilingual query expansion (free, no LLM) — helps Korean queries find English docs.
    # 2026-04-16 R-3 fix: original bug joined variants into a mixed-language
    # blob (broken on multilingual-e5); April-16 Tier-1 patch dropped
    # concatenation but also dropped the alternates entirely. Final:
    # primary query stays single-language; alternates are stashed on the
    # `bilingual_variants` list so _search_rag can fan out and RRF-fuse
    # per variant. Preserves Korean-query → English-doc bridge without
    # corrupting the primary embedding.
    bilingual_variants: list[str] = []
    try:
        if _RAG_IN_PROCESS:
            _v = _rag_search.expand_query(query)
            if _v and len(_v) > 1:
                query = _v[0]
                bilingual_variants = [x for x in _v[1:] if x and x != query][:2]
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
        # 2026-04-17 perf: raw-* types live exclusively in the `experience`
        # collection (verified 11,700 rows vs 0 elsewhere). Previously this
        # applied `$nin` filter to every collection's query, adding ~25ms per
        # call × 13 collections ≈ 320ms waste on a filter most collections
        # don't even need. Split the fan-out: filter only the collection that
        # actually holds raw rows; query the rest unfiltered.
        _raw_collections = {"experience"}
        # Resolve the actual collection list — None means "search all known".
        _active_cols = list(collections) if collections else list(_ALL_COLLECTIONS)
        filtered_cols = [c for c in _active_cols if c in _raw_collections]
        plain_cols = [c for c in _active_cols if c not in _raw_collections]

        # Preserve user-supplied `where` (source_type etc.) on plain side —
        # strip only the auto-injected raw_exclude.
        plain_where: dict | None = None
        if source_type:
            # Caller explicitly scoped by type — that filter belongs on every
            # collection, so plain_where carries it.
            plain_where = local_where

        # Run filtered + plain fan-outs concurrently. Two-call split only
        # pays off if the two halves overlap; otherwise this is sequential
        # waste. Shared threadpool handles both; hybrid_search internally
        # already parallelizes across its own collections.
        _pool = _search_rag_split_pool
        _futs = []
        if filtered_cols:
            _futs.append(_pool.submit(search_rag, query, limit * 2, local_where or None, filtered_cols))
        if plain_cols:
            _futs.append(_pool.submit(search_rag, query, limit * 2, plain_where or None, plain_cols))
        # 2026-04-16 R-3: bilingual-variant expansion in parallel with the
        # primary fan-outs — variants are independent queries so we can run
        # them concurrently instead of after the fact.
        try:
            for _alt in (bilingual_variants or [])[:1]:
                if filtered_cols:
                    _futs.append(_pool.submit(search_rag, _alt, limit, local_where or None, filtered_cols))
                if plain_cols:
                    _futs.append(_pool.submit(search_rag, _alt, limit, plain_where or None, plain_cols))
        except Exception:
            pass

        raw_results = []
        for _fut in _futs:
            try:
                # 2026-04-17 fix: bound fan-out so a hung Chroma doesn't
                # wedge /recall/v2 forever. 15s is well above warm p95.
                _r = _fut.result(timeout=15)
                if _r:
                    raw_results.extend(_r)
            except Exception:
                continue

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
                            f"SELECT chroma_id, tier, superseded_by, valid_from, valid_until, "
                            f"       provisional, trust_score, speaker_entity, scope "
                            f"FROM atoms WHERE chroma_id IN ({placeholders})",
                            sm_ids,
                        ).fetchall()
                    atoms_meta_map = {row["chroma_id"]: dict(row) for row in rows}
        except Exception:
            atoms_meta_map = {}

        # Phase 1B/1C/1D + v3 Layer D: semantic_memory lifecycle + hygiene filters.
        # v3 hygiene filters default STRICT per Chris's decision 3: brain
        # defaults to Chris's trusted statements only (not agent inferences).
        # Each filter has its own escape hatch (F6 fix 2026-04-14) — see
        # search_all docstring.
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

                # v3 Layer D: hygiene filters. Only fires when atom_row exists
                # (post-migration-10 atoms). Pre-migration atoms don't have
                # these fields so they pass through unfiltered — gradual
                # rollout without breaking historical data.
                if atom_row:
                    # Filter 1 — provisional excluded by default.
                    # 2026-04-16 R-2: also unconditionally excludes
                    # kind='conjecture' unless include_provisional is set,
                    # so dream_replay's generative atoms never leak into
                    # factual recall. Conjectures are explicitly
                    # addressable via brain_recall with include_provisional=true.
                    if not include_provisional and atom_row.get("provisional"):
                        continue
                    if not include_provisional and (atom_row.get("kind") or "").lower() == "conjecture":
                        continue
                    # Filter 2 — speaker entity must be Chris (direct statement)
                    speaker = atom_row.get("speaker_entity") or "chris"
                    if not include_all_speakers:
                        if speaker != "chris" and not speaker.startswith("canonical"):
                            continue
                    # Filter 3 — scope must be global or project (not session)
                    atom_scope = atom_row.get("scope") or "global"
                    if atom_scope == "session" and not include_session_scope:
                        continue
                    # Filter 4 — trust_score floor at 0.3
                    ts_val = atom_row.get("trust_score")
                    if ts_val is not None and float(ts_val) < 0.3 and not include_low_trust:
                        continue
                    # Filter 5 — valid_until in the future or NULL
                    vu_full = atom_row.get("valid_until") or ""
                    if vu_full and not include_expired:
                        import datetime as _dt

                        try:
                            vu_dt = _dt.datetime.fromisoformat(vu_full.replace("Z", "+00:00"))
                            if vu_dt.tzinfo is None:
                                vu_dt = vu_dt.replace(tzinfo=_dt.UTC)
                            if vu_dt < _dt.datetime.now(_dt.UTC):
                                continue
                        except (ValueError, TypeError):
                            pass
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
            # 2026-04-16 R-3: graph_search returns dicts without a `path`
            # key, so RRF fusion (id_key="path") fell back to the
            # content-hash anonymous bucket and cross-source agreement was
            # lost for graph hits. Inject a deterministic synthetic path
            # derived from the atom/entity id so graph results fuse
            # correctly with rag/canonical hits on the same atom.
            for r in res or []:
                if isinstance(r, dict) and not r.get("path"):
                    _rid = r.get("id") or r.get("memory_id") or r.get("entity") or ""
                    if _rid:
                        r["path"] = f"graph://{_rid}"
        except Exception:
            res = []
        source_timing["graph_ms"] = int((time.time() - t0) * 1000)
        return res

    def _search_fts():
        t0 = time.time()
        res: list[dict] = []
        # 2026-04-17 T2.9: query both FTS indexes — Chroma-synced (fts_index, nightly rebuild)
        # AND live raw_events FTS (triggered on brain.db writes). The live index catches
        # literal-wording queries for env vars, model names, error messages that appear
        # verbatim in raw event streams but are excluded from the semantic fan-out.
        try:
            from fts_index import search_fts

            res = search_fts(query, limit=limit) or []
        except Exception:
            res = []
        try:
            from raw_events_fts import search as _raw_fts_search

            raw_hits = _raw_fts_search(query, limit=max(3, limit // 2)) or []
            if raw_hits:
                res = list(res) + list(raw_hits)
        except Exception:
            pass
        source_timing["fts_ms"] = int((time.time() - t0) * 1000)
        return res

    def _search_graph_prefetch():
        if not _entity_query or collections:
            return []
        t0 = time.time()
        res = _prefetch_graph_neighbors(relevance_query, limit=5)
        source_timing["graph_prefetch_ms"] = int((time.time() - t0) * 1000)
        return res

    def _search_raptor():
        """2026-04-16 R-1: query the RAPTOR hierarchical summary tree for
        broad/multi-aspect queries. Pulls level≥1 summary nodes alongside
        leaf canonical so wide queries can retrieve at the right
        abstraction (Sarthi 2024). Does nothing when the canonical_raptor
        collection is empty (pre-first-build state)."""
        if collections:
            return []
        # Cheap heuristic: only query RAPTOR when the query looks broad —
        # > 4 tokens or contains comparison/pattern words. Single-fact
        # queries don't benefit from level-2 summaries.
        q_lower = (relevance_query or "").lower()
        token_count = len(q_lower.split())
        is_broad = token_count > 4 or any(
            w in q_lower
            for w in (
                "overall",
                "pattern",
                "summary",
                "history",
                "philosophy",
                "approach",
                "compare",
                "difference",
                "trend",
                "evolution",
                "strategy",
                "state of",
                "what is chris",
                "how does chris",
            )
        )
        if not is_broad:
            return []
        t0 = time.time()
        try:
            # 2026-04-17 fix: these helpers live in search.py (imported as
            # _rag_search) but were never imported into module scope, so the
            # bare calls below raised NameError every time, caught by the
            # outer except. RAPTOR results silently never entered RRF fusion.
            from search import get_collections, get_embedding, vector_search

            col_map = get_collections()
            col_id = col_map.get("canonical_raptor")
            if not col_id:
                return []
            try:
                emb = get_embedding(query, use_cache=True, prefix="query")
            except Exception:
                return []
            data = vector_search(col_id, emb, n=min(limit, 5))
            if not data:
                return []
            ids = (data.get("ids") or [[]])[0]
            docs = (data.get("documents") or [[]])[0]
            metas = (data.get("metadatas") or [[]])[0]
            dists = (data.get("distances") or [[]])[0]
            out: list[dict] = []
            for i in range(len(docs)):
                vector_sim = max(0.0, min(1.0, 1 - (dists[i] if i < len(dists) else 1.0)))
                level = int((metas[i] or {}).get("level", 1) or 1)
                title = f"RAPTOR summary L{level}"
                out.append(
                    {
                        "id": ids[i] if i < len(ids) else "",
                        "path": f"raptor://{ids[i]}" if i < len(ids) else "raptor://",
                        "title": title,
                        "content": docs[i] or "",
                        "collection": "canonical_raptor",
                        "type": "raptor-summary",
                        "score": round(vector_sim * 100.0, 2),
                        "vector_score": round(vector_sim, 4),
                        "trust_tier": 3,  # derived from canonical, inherit trust
                        "metadata": metas[i] or {},
                    }
                )
            return out
        except Exception:
            return []
        finally:
            source_timing["raptor_ms"] = int((time.time() - t0) * 1000)

    # Phase D2 follow-up: temporal_events direct lookup was net-negative
    # (-0.7pt to -1.0pt content_hit on extended). raw_events content is
    # unsummarized and didn't match the eval's semantic expected_content.
    # Reverted to source-boost-only (see _classify_intent personal/experience
    # bumps). The temporal_router module stays for future use cases.

    # HR5 reverted (2026-04-14): graph/fts/graph_prefetch are always-on
    # orthogonal sources by default — the /recall/v2 default
    # sources=["rag","canonical","obsidian"] relies on them for
    # 97.1% content-hit. But when the caller explicitly asks for
    # canonical-only mode (canonical_first=True → sources=["canonical"])
    # the llm-wiki contract is "truth layer only, no retrieval noise";
    # gating graph/fts/graph_prefetch here makes that hard rule real.
    # Fix 2026-04-16: previously even canonical_first mixed FTS + graph
    # results in, defeating the whole point of the flag.
    _canonical_only = list(sources) == ["canonical"]
    raptor_results: list[dict] = []
    search_fns = [
        (_search_rag, "rag"),
        (_search_canonical, "canonical"),
        (_search_obsidian, "obsidian"),
    ]
    if not _canonical_only:
        search_fns.extend(
            [
                (_search_graph, "graph"),
                (_search_fts, "fts"),
                (_search_graph_prefetch, "graph_prefetch"),
                (_search_raptor, "raptor"),  # 2026-04-16 R-1
            ]
        )
    result_lists = {
        "rag": rag_results,
        "canonical": canonical_results,
        "obsidian": obsidian_results,
        "graph": graph_results,
        "fts": fts_results,
        "graph_prefetch": graph_prefetch_results,
        "raptor": raptor_results,
    }

    future_map = {_search_fanout_pool.submit(fn): name for fn, name in search_fns}
    # 2026-04-17 fix: as_completed can block forever if one source hangs.
    # Wall-clock bound the fan-out so /recall/v2 never wedges on a dead dep.
    _FANOUT_DEADLINE_S = 15
    try:
        for fut in as_completed(future_map, timeout=_FANOUT_DEADLINE_S):
            try:
                result_lists[future_map[fut]].extend(fut.result(timeout=1))
            except Exception:
                pass
    except Exception:
        # timeout on as_completed — gather whatever already completed
        for fut, name in future_map.items():
            if fut.done():
                try:
                    result_lists[name].extend(fut.result(timeout=0.1))
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

    # RRF fusion across sources with trust-based weights.
    # 2026-04-17: per-query trace log. Records source counts + timings +
    # trust weights whenever BRAIN_SEARCH_TRACE=1 env is set OR the query
    # hits a "long tail" (no source returned >3 results). The trace lands
    # in logs/search_trace.jsonl and is fundamental for debugging
    # relevance regressions post-hoc ("why did canonical win over RAG
    # for query X?").
    _source_counts = {
        "rag": len(rag_results),
        "canonical": len(canonical_results),
        "obsidian": len(obsidian_results),
        "graph": len(graph_results),
        "fts": len(fts_results),
        "graph_prefetch": len(graph_prefetch_results),
        # 2026-04-18: previously missing — RAPTOR hits were fetched, timed,
        # and dropped silently. The trace log never reported RAPTOR counts,
        # and the RRF fusion below didn't include them either.
        "raptor": len(raptor_results),
    }
    try:
        from rrf import rrf_fuse

        # 2026-04-18: raptor_results was fetched (_search_raptor populates it
        # via the fan-out pool at line 1288) but never flowed into RRF —
        # source_lists and trust_weights both omitted it. Every broad query
        # wasted the RAPTOR fetch and lost the hierarchical-summary signal.
        source_lists = [
            l
            for l in [
                rag_results,
                canonical_results,
                obsidian_results,
                graph_results,
                fts_results,
                graph_prefetch_results,
                raptor_results,
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
        if raptor_results:
            # canonical-derived, inherit canonical trust a shade below canonical itself.
            trust_weights.append(0.85 * _intent_boost.get("canonical", 1.0))
        if source_lists:
            all_results = rrf_fuse(source_lists, trust_weights=trust_weights, id_key="path")
        else:
            all_results = []
        _fusion_mode = "rrf"
    except ImportError:
        all_results = (
            rag_results
            + canonical_results
            + obsidian_results
            + graph_results
            + fts_results
            + graph_prefetch_results
            + raptor_results
        )
        all_results.sort(key=lambda x: (x["score"], x["trust_tier"]), reverse=True)
        trust_weights = []
        _fusion_mode = "fallback_sort"

    # Emit search trace (best-effort, never blocks the request)
    try:
        _maybe_emit_search_trace(
            query=query,
            original_query=original_query,
            source_counts=_source_counts,
            source_timing=source_timing,
            trust_weights=trust_weights,
            fusion_mode=_fusion_mode,
            total_after_fusion=len(all_results),
            intent_boost=_intent_boost,
        )
    except Exception as _exc:
        log.debug("search trace emit failed: %s", _exc)

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

    # 2026-04-17: Emotional valence boost (biological: amygdala).
    # Small multiplicative boost based on Chris's past positive/negative
    # signals on individual atoms. Bounded to ±15% so a miscalibrated valence
    # can't dominate cross-encoder signal. Batch SQL (one query per /recall).
    # Fails open — if valence module or brain.db is unavailable, no boost.
    try:
        from valence import get_valence_batch, valence_to_boost

        _atom_ids_for_valence = [r.get("id") for r in unique if isinstance(r, dict) and r.get("id")]
        if _atom_ids_for_valence:
            _val_map = get_valence_batch(_atom_ids_for_valence)
            if _val_map:
                for r in unique:
                    rid = r.get("id")
                    v = _val_map.get(rid)
                    if v is None or v == 0.0:
                        continue
                    boost = valence_to_boost(v)
                    try:
                        r["score"] = max(0.0, min(100.0, float(r.get("score", 0)) * (1.0 + boost)))
                        r["valence"] = round(v, 4)
                        r["valence_boost"] = boost
                    except (TypeError, ValueError):
                        continue
    except Exception:
        pass

    # M7-WS3: apply linked-entity boost AFTER rerank (mirrors spreading
    # activation pattern below). +5pt bonus per matched entity, capped at
    # +15pt total, so it tiebreaks but never overrides cross-encoder.
    if linked_entities:
        # M8 follow-up: filter linked_entities to length >= 4 to avoid
        # short-name false positives (substring matches "ai" inside "pair",
        # "pr" inside "prior", etc). Then enforce word-ish boundaries via
        # whitespace/punctuation neighbors so we don't double-count
        # substrings inside larger tokens.
        safe_entities = [e for e in linked_entities if e and len(e) >= 4]
        for r in unique:
            content_lower = (r.get("content") or r.get("title") or "").lower()
            matched = 0
            for e in safe_entities:
                idx = content_lower.find(e)
                if idx == -1:
                    continue
                before = content_lower[idx - 1] if idx > 0 else " "
                after_pos = idx + len(e)
                after = content_lower[after_pos] if after_pos < len(content_lower) else " "
                if not before.isalnum() and not after.isalnum():
                    matched += 1
            if matched > 0:
                bonus = min(15.0, 5.0 * matched)
                r["score"] = min(100.0, float(r.get("score", 0)) + bonus)
                r["triple_link_matches"] = matched
        # M8 follow-up: re-sort after boost so late_interaction.rerank() sees
        # the correct top-k window
        unique.sort(key=lambda x: float(x.get("score", 0) or 0), reverse=True)

    # M8.6: late-interaction rerank backend swap. Only fires when
    # BRAIN_RERANK_BACKEND=late_interaction (default off). The module is
    # a no-op when the env var isn't set, so adding the call is free.
    try:
        from late_interaction import rerank as _li_rerank

        unique = _li_rerank(relevance_query, unique, top_k=20)
    except Exception:
        pass

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
            from spreading_activation import boost_results_by_activation, warm_session

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
        from config import BRAIN_SALIENCE_RANKING_ENABLED, BRAIN_TRUST_RANKING_ENABLED
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
        from datetime import datetime as _dt

        SALIENCE_BONUS_MAX = 10.0  # bounded — tiebreaks, doesn't dominate
        RECENCY_HALFLIFE_DAYS = 90.0

        def _recency_from_iso(ts: str) -> float:
            if not ts:
                return 0.0
            try:
                dt = _dt.fromisoformat(ts.rstrip("Zz"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                age_days = (_dt.now(UTC) - dt).total_seconds() / 86400
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

    # 2026-04-17 Phase 3: learned-to-rank blend (sklearn LogisticRegression).
    # No-op when BRAIN_LTR_ENABLED=false or weights missing. Applied AFTER
    # time decay so recency + decay are features the model already sees
    # upstream; avoids the module having to re-derive them.
    try:
        from ltr_blend import apply_if_enabled as _ltr_apply

        unique = _ltr_apply(unique)
        unique.sort(key=lambda x: x.get("score", 0), reverse=True)
    except Exception:
        pass

    # 2026-04-16 Tier 2 fix: canonical trust override. trust_tier=3 marks
    # authoritative canonical notes. R-10 calibration: initial CANON_FLOOR
    # at 55 forced weakly-matching canonical into top-K and regressed
    # content_hit@5 by ~2pt (non-canonical answers displaced). Now:
    # additive bonus only, no floor. Canonical still gets a meaningful
    # leg-up over vector-only hits (rerank.trust_boost 1.4× + this +8)
    # without being artificially landed into top-K when irrelevant.
    # 2026-04-18: quality-gate the bonus. Canonical proposal-header chunks
    # ("## Statement\n\nReview this proposed canonical note...") semantically
    # match shell-session / decision queries but don't contain the literal
    # content the query is really after. Extended eval content_hit@5 dropped
    # 70.0% → 47.4% after this bonus landed unconditionally (a37c5b5) because
    # every header chunk got +8 pushing it past raw-command chunks in top-5.
    # See pipeline/search_memory.py:_strip_proposal_boilerplate for the
    # filesystem-path strip that inspired this gate. Root-cause fix is to
    # stop indexing the boilerplate at ingest time; gating here is the
    # surgical read-time repair until that lands.
    CANON_BONUS = 8.0
    for r in unique:
        trust_tier = r.get("trust_tier", 0)
        if not isinstance(trust_tier, (int, float)):
            trust_tier = 0
        if trust_tier >= 3:
            _head = (r.get("content") or "")[:400]
            if "Review this proposed canonical note" in _head or _head.lstrip().startswith("## Statement"):
                continue
            r["score"] = float(r.get("score", 0)) + CANON_BONUS
            _dbg = dict(r.get("_debug") or {})
            _dbg["canonical_trust_bonus"] = CANON_BONUS
            r["_debug"] = _dbg
    unique.sort(key=lambda x: x.get("score", 0), reverse=True)

    # Preference recency boost: for semantic_memory preferences, newer ones
    # get a significant boost to prevent stale preferences from dominating
    # via accumulated access_count reinforcement.
    now_utc = datetime.now(UTC)

    def _infer_category(r: dict, content: str) -> str:
        """2026-04-16 Tier 2 fix: older memories lack explicit `category`
        metadata so the preference recency boost below never fired. Infer
        from content + existing meta signals. Cheap keyword heuristic.
        """
        existing = (r.get("metadata") or {}).get("category")
        if existing:
            return existing
        lower = (content or "").lower()
        if any(kw in lower for kw in (" prefer", "prefer ", "preference", "선호", "좋아", "원해")):
            return "preference"
        if any(kw in lower for kw in ("decide", "decision", "will use", "going with", "결정")):
            return "decision"
        if any(kw in lower for kw in ("fact:", "is a ", "lives in ", "runs on ")):
            return "fact"
        return ""

    for r in unique:
        collection = r.get("collection", "")
        if collection != "semantic_memory":
            continue
        meta = r.get("metadata") or {}
        # 2026-04-16 Tier 2: infer category when metadata lacks it so
        # older unlabeled preference memories still decay appropriately.
        category = meta.get("category") or _infer_category(r, r.get("content", ""))
        if category != "preference":
            continue
        created_at_raw = meta.get("created_at") or meta.get("updated_at")
        if not created_at_raw:
            continue
        try:
            ts = created_at_raw.replace("Z", "+00:00")
            created_dt = datetime.fromisoformat(ts)
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=UTC)
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
                # 2026-04-16 R-7: use thread-local pooled autonomy.db
                # connection (was new connect() per recall).
                _conn = _get_autonomy_conn()
                if _conn is not None:
                    top_n = unique[:limit]
                    top_ids = [
                        r.get("id") or r.get("path", "") for r in top_n if r.get("id") or r.get("path")
                    ]
                    if top_ids:
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
                            # 2026-04-16 R-7: pooled connection — do NOT close
                            pass
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
    # 2026-04-16 Tier 2 fix: confidence-skip gate removed. Old logic only
    # ran MMR when nth/top >= 0.85 — i.e. only when results were already
    # near-identical, which is precisely the case where a lambda=0.85 MMR
    # does nothing meaningful. With lambda=0.6, MMR now runs whenever
    # there's enough surplus (len > limit) and produces real diversity on
    # well-separated top-k, not just on the already-tied edge case.
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
        # 2026-04-16 R-7: early-exit when marginal gain falls below
        # EARLY_EXIT_GAIN. For well-separated results the tail of the MMR
        # selection contributes vanishing signal — cutting early drops
        # O(k*n) latency with no measurable quality impact.
        EARLY_EXIT_GAIN = 0.02
        prev_best = None
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
            # Early exit: marginal gain vs prev iteration too small.
            # 2026-04-18: previously appended best_idx before breaking, so the
            # item whose gain was BELOW threshold still made it into the
            # selected set — off-by-one against the "stop when below threshold"
            # intent. Now break without including it so MMR selection matches
            # the threshold semantics.
            if prev_best is not None and (prev_best - best_score) < EARLY_EXIT_GAIN:
                break
            prev_best = best_score
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
                # 2026-04-16 Tier 2 fix: previous impl used
                # `toLower(text) CONTAINS toLower(e.name)` with entity
                # names as short as 2 chars. A 2-char entity like "ai"
                # matched inside "pair", "chain", "aim" — dense noisy
                # RELATES_TO edges polluted graph_entity_boost and
                # spreading activation. Now:
                #   1. min entity length = 4 chars (filters false-positive
                #      substrings)
                #   2. word-boundary regex so "ai" still matches "the AI"
                #      but not "chain"
                #   3. per-query hard cap (3 edges) preserved
                import re

                from entity_graph import _use_neo4j

                if not _use_neo4j():
                    return
                from neo4j_client import run_query as _rq
                from neo4j_client import run_write as _rw

                result_text = " ".join(
                    (r.get("content", "") + " " + r.get("title", ""))[:200] for r in final_results[:5]
                ).lower()
                if len(result_text) < 50:
                    return
                # Pull entity candidates with length floor first so the
                # Cypher job returns a tractable set; the expensive
                # word-boundary check then runs Python-side.
                matched = _rq(
                    "MATCH (e:Entity) WHERE size(e.name) >= 4 "
                    "AND toLower($text) CONTAINS toLower(e.name) "
                    "RETURN e.name AS name LIMIT 50",
                    {"text": result_text},
                )
                names: list[str] = []
                for m in matched:
                    name = m["name"]
                    if not name:
                        continue
                    # Require word boundary on at least one side — prevents
                    # matching the name as a substring inside a larger token.
                    pattern = r"(?:^|[^a-z0-9])" + re.escape(name.lower()) + r"(?:[^a-z0-9]|$)"
                    if re.search(pattern, result_text):
                        names.append(name)
                if len(names) < 2:
                    return
                pairs_done = 0
                for i, a in enumerate(names[:5]):
                    for b in names[i + 1 : 5]:
                        if a != b and pairs_done < 3:
                            from datetime import datetime as _dt

                            _rw(
                                "MATCH (s:Entity {name: $a}) "
                                "MATCH (t:Entity {name: $b}) "
                                "MERGE (s)-[r:RELATES_TO {relationship: 'co_retrieved'}]->(t) "
                                "ON CREATE SET r.weight = 0.05, r.co_occurrence_count = 1, r.created_at = $now "
                                "ON MATCH SET r.co_occurrence_count = r.co_occurrence_count + 1, "
                                "  r.weight = CASE WHEN r.weight + (0.05 * (1.0 - r.weight)) > 1.0 THEN 1.0 "
                                "  ELSE r.weight + (0.05 * (1.0 - r.weight)) END",
                                {"a": a, "b": b, "now": _dt.now(UTC).isoformat(timespec="seconds")},
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
