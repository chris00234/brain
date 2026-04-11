"""brain_core/spreading_activation.py — Round 10 A1.

Personalized PageRank over the Neo4j entity graph as a biologically-plausible
spreading-activation retrieval primer. Mirrors HippoRAG (NeurIPS '24) and
HippoRAG 2 (ICML '25), both of which use PPR on a knowledge graph as the
hippocampal indexing mechanism.

The flow:
  1. Extract candidate entities from the user's query (seed nodes)
  2. Run Personalized PageRank on a cached projection of Neo4j's RELATES_TO
     subgraph, biased toward the seeds
  3. Return a {entity_name: activation_score} map. Downstream callers boost
     result rankings for memories whose metadata.entities overlap activated
     entities.

The graph projection is cached for ~5 minutes — Neo4j Bolt fetches are slow
relative to PPR computation. Activation maps themselves are session-scoped
in autonomy.db so follow-up queries inside a session inherit warmth.

Reference: https://arxiv.org/abs/2405.14831 (HippoRAG)
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("brain.spreading_activation")

try:
    import networkx as nx
except ImportError:
    nx = None  # graceful degradation — module is a no-op without networkx

try:
    from config import BRAIN_LOGS_DIR
except ImportError:
    BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")

ACTIVATION_DB = BRAIN_LOGS_DIR / "autonomy.db"

# Graph projection cache — Neo4j queries are 100-300ms; PPR over a 1000-node
# in-memory graph is sub-millisecond. Refresh every 5 minutes.
_graph_cache: dict[str, Any] = {"graph": None, "fetched_at": 0.0}
_graph_lock = threading.Lock()
_GRAPH_TTL_SECONDS = 300

# PPR hyperparameters
ALPHA = 0.85       # standard damping factor
MAX_ITER = 30      # cap iterations to bound latency
TOLERANCE = 1e-4   # convergence threshold

# Activation TTL in the autonomy.db session table
ACTIVATION_TTL_SECONDS = 90


def _ensure_table() -> None:
    ACTIVATION_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(ACTIVATION_DB))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS entity_activation (
                session_id TEXT NOT NULL,
                entity_name TEXT NOT NULL,
                activation REAL NOT NULL,
                expires_at REAL NOT NULL,
                PRIMARY KEY (session_id, entity_name)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_activation_expires ON entity_activation(expires_at)")
        conn.commit()
    finally:
        conn.close()


_table_initialized = False
_table_lock = threading.Lock()


def _ensure_table_once() -> None:
    global _table_initialized
    if _table_initialized:
        return
    with _table_lock:
        if _table_initialized:
            return
        _ensure_table()
        _table_initialized = True


def _fetch_neo4j_edges(max_edges: int = 5000) -> list[tuple[str, str, float]]:
    """Pull (source, target, weight) tuples from Neo4j. Best-effort: returns
    empty list if Neo4j is unreachable so the caller falls back gracefully."""
    try:
        from neo4j_client import run_query
    except Exception:
        return []
    try:
        rows = run_query(
            "MATCH (a:Entity)-[r:RELATES_TO]-(b:Entity) "
            "RETURN a.name AS src, b.name AS dst, coalesce(r.weight, 0.5) AS w "
            "LIMIT $limit",
            {"limit": max_edges},
        )
        return [(r["src"], r["dst"], float(r["w"])) for r in rows if r.get("src") and r.get("dst")]
    except Exception as e:
        log.debug("neo4j edge fetch failed: %s", e)
        return []


def _build_graph() -> Any:
    """Build a NetworkX DiGraph from cached Neo4j edges. Returns None if
    networkx is missing or no edges available."""
    if nx is None:
        return None
    edges = _fetch_neo4j_edges()
    if not edges:
        return None
    g = nx.DiGraph()
    for src, dst, w in edges:
        # RELATES_TO is undirected in Neo4j but PPR works on a directed graph;
        # add both directions so spreading propagates symmetrically.
        if g.has_edge(src, dst):
            g[src][dst]["weight"] = max(g[src][dst]["weight"], w)
        else:
            g.add_edge(src, dst, weight=w)
        if g.has_edge(dst, src):
            g[dst][src]["weight"] = max(g[dst][src]["weight"], w)
        else:
            g.add_edge(dst, src, weight=w)
    return g


def _get_graph() -> Any:
    """Cached graph getter with TTL. Holds the lock for the full check-and-build
    so concurrent callers under TTL expiry don't trigger N redundant Neo4j
    fetches — first thread builds, others wait then read the cache.
    """
    now = time.time()
    with _graph_lock:
        if _graph_cache["graph"] is not None and (now - _graph_cache["fetched_at"]) < _GRAPH_TTL_SECONDS:
            return _graph_cache["graph"]
        g = _build_graph()
        _graph_cache["graph"] = g
        _graph_cache["fetched_at"] = now
        return g


def _seed_entities_from_query(query: str, max_seeds: int = 8) -> list[str]:
    """Cheap entity extraction: nodes in the cached graph whose name appears
    as a *whole word* in the query (case-insensitive). Word-boundary regex
    avoids spurious matches like "go" → "google", "ago", "argo".
    """
    import re as _re
    g = _get_graph()
    if g is None:
        return []
    q = (query or "").lower()
    if len(q) < 3:
        return []
    # Tokenize the query once into a set of word-tokens for O(1) lookup.
    # This is dramatically faster than running 1000+ regex searches per node.
    q_tokens = set(_re.findall(r"\w+", q))
    matches: list[tuple[str, int]] = []
    for node in g.nodes():
        if not isinstance(node, str) or len(node) < 3:
            continue
        node_lower = node.lower()
        # Multi-word entity (e.g. "chris cho"): match if every word is in query
        node_tokens = node_lower.split()
        if len(node_tokens) > 1:
            if all(t in q_tokens for t in node_tokens):
                matches.append((node, len(node)))
            continue
        # Single-word entity: must be in the query token set as a whole word
        if node_lower in q_tokens:
            matches.append((node, len(node)))
    matches.sort(key=lambda kv: -kv[1])
    return [m[0] for m in matches[:max_seeds]]


def activate(query: str, top_k: int = 20) -> dict[str, float]:
    """Run Personalized PageRank biased toward query-matched entities.

    Returns a {entity_name: activation_score} map, capped at top_k entries
    with the highest PPR scores. Empty dict on failure (caller treats as
    'no activation' and proceeds with normal retrieval).
    """
    if nx is None:
        return {}
    g = _get_graph()
    if g is None or len(g) == 0:
        return {}
    seeds = _seed_entities_from_query(query)
    if not seeds:
        return {}
    personalization = {n: 0.0 for n in g.nodes()}
    seed_weight = 1.0 / len(seeds)
    for s in seeds:
        if s in personalization:
            personalization[s] = seed_weight
    try:
        scores = nx.pagerank(
            g,
            alpha=ALPHA,
            personalization=personalization,
            max_iter=MAX_ITER,
            tol=TOLERANCE,
            weight="weight",
        )
    except nx.PowerIterationFailedConvergence:
        log.debug("PPR did not converge in %d iterations; using partial result", MAX_ITER)
        return {}
    except Exception as e:
        log.debug("PPR failed: %s", e)
        return {}
    # Filter out the seeds themselves (they're already in the query) and
    # sort by activation score descending.
    seed_set = set(seeds)
    ranked = sorted(
        ((n, s) for n, s in scores.items() if n not in seed_set and s > 0),
        key=lambda kv: -kv[1],
    )[:top_k]
    if not ranked:
        return {}
    # Min-max normalize to [0, 1] so the boost multiplier is bounded.
    max_score = ranked[0][1]
    if max_score <= 0:
        return {}
    return {n: round(s / max_score, 4) for n, s in ranked}


def store_session_activation(session_id: str, activation: dict[str, float]) -> None:
    """Persist activation map for the session so follow-up queries inherit warmth."""
    if not session_id or not activation:
        return
    _ensure_table_once()
    expires = time.time() + ACTIVATION_TTL_SECONDS
    conn = sqlite3.connect(str(ACTIVATION_DB))
    try:
        # Sweep expired rows on every write — keeps the table small.
        conn.execute("DELETE FROM entity_activation WHERE expires_at < ?", (time.time(),))
        conn.executemany(
            "INSERT OR REPLACE INTO entity_activation (session_id, entity_name, activation, expires_at) VALUES (?, ?, ?, ?)",
            [(session_id, n, a, expires) for n, a in activation.items()],
        )
        conn.commit()
    finally:
        conn.close()


def load_session_activation(session_id: str) -> dict[str, float]:
    """Read non-expired activation rows for a session."""
    if not session_id:
        return {}
    _ensure_table_once()
    conn = sqlite3.connect(str(ACTIVATION_DB))
    try:
        rows = conn.execute(
            "SELECT entity_name, activation FROM entity_activation "
            "WHERE session_id = ? AND expires_at >= ?",
            (session_id, time.time()),
        ).fetchall()
    finally:
        conn.close()
    return {n: a for n, a in rows}


def merge_activation(*maps: dict[str, float]) -> dict[str, float]:
    """Combine multiple activation maps, taking the max per entity.

    Used to fuse the current query's PPR result with the session's prior
    warmth so the recall ranker sees the union of activated entities.
    """
    out: dict[str, float] = {}
    for m in maps:
        if not m:
            continue
        for k, v in m.items():
            if v > out.get(k, 0):
                out[k] = v
    return out


def boost_results_by_activation(
    results: list[dict],
    activation: dict[str, float],
    bonus_max: float = 5.0,
    top_n: int | None = None,
) -> list[dict]:
    """Add an activation bonus to each result's score, capped at bonus_max.

    Round 11: switched from multiplicative to additive (Wave 1.5 lesson —
    multiplicative boosts perturb top-N rankings; additive caps the impact).

    Looks for activated entity names in (a) `metadata.entities` if present,
    (b) the result content/title as a substring fallback. Substring matches
    use word-boundary token sets (no spurious "go" → "google").

    `top_n` limits how many results get touched (so we don't waste cycles
    on low-rank candidates that won't be returned anyway).

    Mutation is in-place; the list is also returned.
    """
    if not activation or not results:
        return results
    import re as _re
    # Pre-lowercase + tokenize the activation keys
    activation_lower = {k.lower(): v for k, v in activation.items() if k and len(k) >= 3}
    pool = results if top_n is None else results[:top_n]
    for r in pool:
        if not isinstance(r, dict):
            continue
        best = 0.0
        # Path A: explicit metadata.entities
        meta = r.get("metadata") or {}
        entities = meta.get("entities") or []
        if isinstance(entities, str):
            entities = [e.strip() for e in entities.split(",") if e.strip()]
        for ent in entities:
            score = activation.get(ent, 0.0)
            if score > best:
                best = score
        # Path B: word-boundary substring fallback for results without
        # explicit entity tagging. Tokenize the haystack once, check
        # whole-word membership against each activation key.
        if best == 0 and activation_lower:
            haystack = (
                (r.get("title", "") or "")[:200]
                + " "
                + (r.get("content", "") or "")[:400]
            ).lower()
            if haystack:
                tokens = set(_re.findall(r"\w+", haystack))
                for ent_lower, score in activation_lower.items():
                    parts = ent_lower.split()
                    if len(parts) > 1:
                        if all(p in tokens for p in parts) and score > best:
                            best = score
                    else:
                        if ent_lower in tokens and score > best:
                            best = score
        if best > 0:
            try:
                r["score"] = float(r.get("score", 0)) + bonus_max * best
                r["activation_boost"] = round(best, 4)
            except (TypeError, ValueError):
                pass
    return results


def warm_session(session_id: str, query: str) -> dict[str, float]:
    """One-call helper used by the recall path: compute fresh activation for
    the query, merge with the session's prior warmth, persist, and return
    the merged map for downstream boost.
    """
    fresh = activate(query)
    prior = load_session_activation(session_id) if session_id else {}
    merged = merge_activation(prior, fresh)
    if session_id and merged:
        store_session_activation(session_id, merged)
    return merged
