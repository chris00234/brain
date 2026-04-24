"""brain_core/triple_link.py — HippoRAG2-style query-to-triple linking (M7-WS3).

The classic HippoRAG path is NER → node match → 1-hop expansion. The HippoRAG 2
paper (arXiv:2502.14802) showed +12.5% Recall@5 by embedding the query directly
and matching against pre-embedded entity-relationship-entity triples instead of
hopping through NER. This module is a minimal port for the brain.

Pipeline:
  1. Extract all (entity_a, rel, entity_b) triples from Neo4j (cached for 5 min)
  2. Embed each triple as "entity_a rel entity_b" via the local Ollama embedder
  3. Embed the incoming query (passage prefix)
  4. Cosine-similarity match query → top K triples
  5. Return the (entity_a, entity_b) names from those triples — callers can
     boost results whose content mentions any of those entity names

Wire-up: search_unified.search_all calls get_query_linked_entities() before
the RRF stage. Boost factor lives in rerank.py.

Master kill switch: BRAIN_TRIPLE_LINK_ENABLED env var (default false during
WS3 ramp-up; flip to true once measured to lift content_hit on extended).
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

log = logging.getLogger("brain.triple_link")

ENABLED = os.environ.get("BRAIN_TRIPLE_LINK_ENABLED", "").lower() in {"1", "true", "yes"}
TRIPLE_CACHE_TTL_S = 300  # 5 minutes — Neo4j triples don't change every request
# M8 follow-up: lowered from 5000 → 1500. The mention_count > 1 filter on the
# Neo4j side already removes long-tail entities, and 1500 triples x 1024-dim
# embeddings x 4 bytes = ~6MB resident — small enough to barely register, but
# the SAVINGS is on first-load wall-clock: 5000 sequential embed calls ~150s
# vs 1500 ~45s. Same recall on the typical query because top-K of 10 is
# usually saturated by the first few hundred most-connected entities anyway.
MAX_TRIPLES = 1500
TOP_K = 5  # how many query→triple matches to consider per request (was 10)
# M8 follow-up: raised from 0.55 → 0.72. At 0.55 the query-to-triple
# cosine was matching semantically-loose pairs on e5 (which tends to
# produce ~0.5 for any tangentially related phrase), which then boosted
# off-topic docs whose content mentioned those entities. 0.72 is
# empirically much more selective. Live A/B on extended:
#   off       = 73.8% source_hit
#   on@0.55   = 72.3%
#   on@0.72   = TBD (if still degrades, disable the feature)
MIN_SIMILARITY = 0.72


# Module-level cache: list of (triple_str, embedding, entity_a, entity_b)
_TripleCache = list[tuple[str, list[float], str, str]]
_cache: _TripleCache = []
_cache_loaded_at: float = 0.0


def _embed_passage(text: str) -> list[float] | None:
    """Embed a triple (entity rel entity) — uses passage prefix per e5 contract."""
    try:
        from indexer import get_embedding

        return get_embedding(text, prefix="passage")
    except Exception as exc:
        log.warning("triple_link passage embed failed: %s", exc)
        return None


def _embed_query(text: str) -> list[float] | None:
    """Embed an incoming query — uses query prefix per e5 contract.

    M8 follow-up: split from the single _ollama_embed helper that hardcoded
    `prefix="passage"` for both. That was wrong on an asymmetric model and
    would underperform by several recall points the moment ENABLED flipped.
    """
    try:
        from indexer import get_embedding

        return get_embedding(text, prefix="query")
    except Exception as exc:
        log.warning("triple_link query embed failed: %s", exc)
        return None


def _load_triples_from_neo4j() -> list[tuple[str, str, str]]:
    """Pull (entity_a, relationship, entity_b) triples ordered by mention count."""
    try:
        from neo4j_client import run_query

        # `a`/`b` get rebound as the RETURN aliases (strings), so referring to
        # `a.mention_count` in ORDER BY after that silently reads property
        # access on a String and Neo4j errors with
        # "expected a map but was String(...)". Include the sum as a column.
        rows = run_query(
            "MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity) "
            "WHERE a.mention_count > 1 AND b.mention_count > 1 "
            "RETURN a.name AS a, coalesce(r.relationship, 'related_to') AS rel, "
            "  b.name AS b, r.weight AS w, "
            "  a.mention_count + b.mention_count AS mc "
            "ORDER BY w DESC, mc DESC "
            "LIMIT $limit",
            {"limit": MAX_TRIPLES},
        )
        return [
            (str(r.get("a", "")), str(r.get("rel", "related_to")), str(r.get("b", "")))
            for r in rows
            if r.get("a") and r.get("b")
        ]
    except Exception as exc:
        log.warning("triple_link neo4j fetch failed: %s", exc)
        return []


def _refresh_cache_if_stale() -> None:
    global _cache, _cache_loaded_at
    now = time.time()
    if _cache and (now - _cache_loaded_at) < TRIPLE_CACHE_TTL_S:
        return

    triples = _load_triples_from_neo4j()
    if not triples:
        _cache_loaded_at = now
        return

    # Deduplicate + drop self-loops before embedding.
    prepared: list[tuple[str, str, str]] = []
    for a, rel, b in triples:
        if not a or not b or a == b:
            continue
        prepared.append((a, rel, b))

    # Batch-embed — serial per-triple Ollama round-trips were ~45s cold-start
    # on 1500 triples; batched /api/embed with batch_size=50 drops to ~3-5s.
    texts = [f"{a} {rel} {b}" for a, rel, b in prepared]
    try:
        from indexer import get_embeddings_batch

        embeddings = get_embeddings_batch(texts, prefix="passage", batch_size=50)
    except Exception as exc:
        log.warning("triple_link batch embed failed, falling back to serial: %s", exc)
        embeddings = [_embed_passage(t) or [] for t in texts]

    new_cache: _TripleCache = []
    for (a, _rel, b), triple_str, emb in zip(prepared, texts, embeddings, strict=False):
        if emb:
            new_cache.append((triple_str, emb, a, b))

    _cache = new_cache
    _cache_loaded_at = now
    log.info("triple_link cache refreshed: %d triples embedded (batched)", len(new_cache))


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def get_query_linked_entities(query: str, top_k: int = TOP_K) -> set[str]:
    """Return the entity names linked to a query via top-K matched triples.

    Returns an empty set when the feature is disabled or no triples match.
    Callers can pass this into rerank to give a small score boost to results
    whose content mentions any returned entity.
    """
    if not ENABLED:
        return set()
    if not query or len(query) < 3:
        return set()

    _refresh_cache_if_stale()
    if not _cache:
        return set()

    q_emb = _embed_query(query)
    if not q_emb:
        return set()

    scored: list[tuple[float, str, str]] = []
    for _triple_str, emb, ent_a, ent_b in _cache:
        sim = _cosine(q_emb, emb)
        if sim >= MIN_SIMILARITY:
            scored.append((sim, ent_a, ent_b))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:top_k]

    linked: set[str] = set()
    for _sim, a, b in top:
        linked.add(a.lower())
        linked.add(b.lower())
    return linked


def stats() -> dict:
    """Lightweight introspection for /brain/diag and tests."""
    return {
        "enabled": ENABLED,
        "cache_size": len(_cache),
        "cache_age_s": int(time.time() - _cache_loaded_at) if _cache_loaded_at else None,
        "top_k": TOP_K,
        "min_similarity": MIN_SIMILARITY,
        "ttl_s": TRIPLE_CACHE_TTL_S,
    }
