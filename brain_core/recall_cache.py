"""In-memory caches used by recall endpoints.

This module intentionally preserves the legacy private names because tests and
route code use them as compatibility seams while ``routes.recall`` is gradually
split into smaller units.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable

from indexer import get_embedding as _default_get_embedding
from recall_models import RecallV2Response

# ── Recall v2 response cache (30s TTL) ──
_recall_cache: dict[str, tuple[float, RecallV2Response]] = {}
_recall_cache_lock = threading.Lock()
_RECALL_CACHE_TTL = 30.0
_RECALL_CACHE_MAX = 100


def _recall_cache_get(key: str) -> RecallV2Response | None:
    with _recall_cache_lock:
        entry = _recall_cache.get(key)
        if entry and (time.time() - entry[0]) < _RECALL_CACHE_TTL:
            return entry[1]
        if entry:
            del _recall_cache[key]
    return None


def _recall_cache_put(key: str, response: RecallV2Response) -> None:
    with _recall_cache_lock:
        _recall_cache[key] = (time.time(), response)
        if len(_recall_cache) > _RECALL_CACHE_MAX:
            oldest = min(_recall_cache, key=lambda k: _recall_cache[k][0])
            del _recall_cache[oldest]


# ── Semantic query cache for /recall (embedding-similarity based, 60s TTL) ──
# Separate lock for the semantic-similarity embedding cache. Sharing the
# response-cache lock meant the cosine scan (O(N*dim)) ran under a contention
# hotspot — every concurrent recall/v2 caller serialized on it.
_recall_emb_lock = threading.Lock()
_recall_embedding_cache: list[
    tuple[float, list[float], str, dict]
] = []  # (timestamp, embedding, query, response)
_RECALL_EMB_TTL = 60.0
_RECALL_EMB_MAX = 50
_RECALL_EMB_SIM_THRESHOLD = 0.92

# 2026-04-16 Tier 2: Matryoshka-style dimension truncation for the recall
# semantic-similarity cache. multilingual-e5-large-instruct emits 1024-dim
# vectors, and the cache's linear scan (~50 entries x 1024 dims per miss)
# paid ~2ms of pure Python cosine work per request on top of the ~60ms
# Ollama embed. Matryoshka Representation Learning (Kusupati 2022) shows
# that truncating an embedding to its first k dimensions + re-normalizing
# preserves near-full retrieval quality at a fraction of the compute.
# 256 dims = 4x faster cosine, measured ≤2% recall loss in literature.
# The threshold is unchanged because cosine on L2-normalized prefixes
# stays comparable to full-vector cosine.
_MATRYOSHKA_DIM = 256
EmbeddingFn = Callable[..., list[float] | None]


def _truncate_normalize(vec: list[float], dim: int = _MATRYOSHKA_DIM) -> list[float]:
    if not vec or len(vec) <= dim:
        return vec
    head = vec[:dim]
    norm = math.sqrt(sum(x * x for x in head))
    if norm <= 0:
        return head
    return [x / norm for x in head]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _recall_emb_cache_lookup(query: str, get_embedding: EmbeddingFn = _default_get_embedding) -> dict | None:
    """Check semantic similarity cache. Returns cached response or None."""
    if not query:
        return None
    try:
        emb = get_embedding(query[:200], use_cache=True, prefix="query")
    except Exception:
        return None
    if not emb:
        return None
    # Truncate + renormalize ONCE per lookup — the cached entries are
    # already stored in their truncated form.
    emb_trunc = _truncate_normalize(emb)
    now = time.time()
    # Snapshot under lock, scan outside. The cosine loop is O(N*dim) ~50k
    # float mults and must not run inside a contention hotspot.
    with _recall_emb_lock:
        _recall_embedding_cache[:] = [e for e in _recall_embedding_cache if now - e[0] < _RECALL_EMB_TTL]
        snapshot = list(_recall_embedding_cache)
    for _ts, cached_emb, _cached_query, resp in snapshot:
        if _cosine(emb_trunc, cached_emb) > _RECALL_EMB_SIM_THRESHOLD:
            return resp
    return None


def _recall_emb_cache_put(
    query: str,
    response: dict,
    get_embedding: EmbeddingFn = _default_get_embedding,
) -> None:
    if not query:
        return
    try:
        emb = get_embedding(query[:200], use_cache=True, prefix="query")
    except Exception:
        return
    if not emb:
        return
    # Store only the truncated + renormalized prefix to match lookup-side.
    emb_trunc = _truncate_normalize(emb)
    now = time.time()
    with _recall_emb_lock:
        # 2026-04-16 R-4: prune by TTL at put time, not just at lookup.
        # Previously lookup-only eviction let expired entries accumulate
        # when reads were sparse, wasting the 50-slot budget and evicting
        # still-valid entries prematurely.
        _recall_embedding_cache[:] = [e for e in _recall_embedding_cache if now - e[0] < _RECALL_EMB_TTL]
        _recall_embedding_cache.append((now, emb_trunc, query, response))
        if len(_recall_embedding_cache) > _RECALL_EMB_MAX:
            _recall_embedding_cache.pop(0)


def clear_caches() -> dict:
    """Clear recall response + embedding caches. Called by /admin/embed_adapter
    after a LoRA adapter swap so A/B comparisons do not serve stale results.
    """
    with _recall_cache_lock:
        n1 = len(_recall_cache)
        _recall_cache.clear()
    with _recall_emb_lock:
        n2 = len(_recall_embedding_cache)
        _recall_embedding_cache.clear()
    return {"recall_cache_cleared": n1, "embedding_cache_cleared": n2}
