"""brain_core/parent_child_expand.py — child → parent content swap (M9.2).

When `semantic_chunk` emits parent-child hierarchy, each child chunk carries
`metadata.parent_id` pointing at its parent chunk. At retrieval time, children
are preferred because they're small and have tight cosine match. But the LLM
consuming the results benefits from seeing the wider parent context.

This module provides `expand_to_parents(results)`: for each result whose
metadata has a non-null `parent_id`, look up the parent chunk content in
ChromaDB and swap it into the result's `content` field (keeping source + id
intact). The child's original content is preserved in
`metadata.child_content` for downstream rerankers that need the precise
match substring.

Cost: one ChromaDB query per unique `parent_id` in the result set. Batched
into a single `where={"chunk_id": {"$in": [...]}}` call so the cost is O(1)
per recall request, not O(N).

Default OFF via BRAIN_PARENT_CHILD_EXPAND env var. Safe rollout because
parent chunks are not yet a dominant fraction of the corpus — only
pdfs.py writes them, and chunk_with_fallback only emits parents when
BRAIN_SEMANTIC_CHUNKING is also on.

In-memory cache: parent_id → content, TTL 5 min. Parent chunks don't
change between ingests, so cache hit rate approaches 100% for repeated
queries against the same PDF.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

log = logging.getLogger("brain.parent_child_expand")

ENABLED = os.environ.get("BRAIN_PARENT_CHILD_EXPAND", "").lower() in {"1", "true", "yes"}

_CACHE: dict[str, tuple[str, float]] = {}
_CACHE_TTL_S = 300
_MAX_CACHE_SIZE = 1000


def _cache_get(parent_id: str) -> str | None:
    entry = _CACHE.get(parent_id)
    if not entry:
        return None
    content, loaded_at = entry
    if time.time() - loaded_at > _CACHE_TTL_S:
        _CACHE.pop(parent_id, None)
        return None
    return content


def _cache_put(parent_id: str, content: str) -> None:
    if len(_CACHE) >= _MAX_CACHE_SIZE:
        # Drop oldest ~10% entries
        sorted_items = sorted(_CACHE.items(), key=lambda kv: kv[1][1])
        for k, _ in sorted_items[: len(_CACHE) // 10]:
            _CACHE.pop(k, None)
    _CACHE[parent_id] = (content, time.time())


def _fetch_parents_from_chroma(parent_ids: list[str]) -> dict[str, str]:
    """Batch-fetch parent chunks from ChromaDB by metadata.chunk_id.

    Returns {parent_id: content}. Misses are silent (empty dict entries
    stay absent so the caller can skip swap for those).
    """
    if not parent_ids:
        return {}
    try:
        from indexer import _get_collection_id, chroma_api
    except Exception as _exc:
        log.warning("parent_child_expand import failed: %s", _exc)
        return {}

    col_id = _get_collection_id("knowledge")
    if not col_id:
        log.warning("parent_child_expand: no collection id for 'knowledge'")
        return {}

    out: dict[str, str] = {}
    try:
        # ChromaDB metadata filter — $in allows batch lookup
        resp = chroma_api(
            "POST",
            f"/api/v2/tenants/default_tenant/" f"databases/default_database/collections/{col_id}/get",
            {
                "where": {"chunk_id": {"$in": parent_ids}},
                "include": ["documents", "metadatas"],
                "limit": len(parent_ids) * 2,
            },
        )
        ids = resp.get("ids", []) or []
        docs = resp.get("documents", []) or []
        metas = resp.get("metadatas", []) or []
        for i, _doc_id in enumerate(ids):
            meta = metas[i] if i < len(metas) else {}
            content = docs[i] if i < len(docs) else ""
            chunk_id = meta.get("chunk_id") if isinstance(meta, dict) else None
            if chunk_id and content:
                out[chunk_id] = content
    except Exception as exc:
        log.warning("parent fetch failed: %s", exc)
    return out


def expand_to_parents(results: list[dict]) -> list[dict]:
    """For each result with a parent_id, swap in parent content.

    Preserves the original child content in `metadata.child_content` so
    downstream code can see the tight-match substring. Skips results whose
    metadata lacks parent_id or whose parent can't be fetched.

    No-op when BRAIN_PARENT_CHILD_EXPAND is unset.
    """
    if not ENABLED or not results:
        return results

    # Collect unique parent_ids from the results (and check cache first)
    uncached: list[str] = []
    want: dict[str, list[int]] = {}  # parent_id → list of result indices
    for i, r in enumerate(results):
        meta = r.get("metadata") or {}
        if not isinstance(meta, dict):
            continue
        pid = meta.get("parent_id")
        if not pid or meta.get("is_parent"):
            continue
        want.setdefault(pid, []).append(i)
        if _cache_get(pid) is None:
            uncached.append(pid)

    if not want:
        return results

    # Batch-fetch uncached parents
    if uncached:
        fetched = _fetch_parents_from_chroma(uncached)
        for pid, content in fetched.items():
            _cache_put(pid, content)

    # Swap in parent content
    swapped = 0
    for pid, indices in want.items():
        parent_content = _cache_get(pid)
        if not parent_content:
            continue
        for i in indices:
            r = results[i]
            meta = r.get("metadata") or {}
            if isinstance(meta, dict):
                meta = dict(meta)
                meta["child_content"] = r.get("content", "")[:2000]
                meta["parent_expanded"] = True
                r["metadata"] = meta
            r["content"] = parent_content
            swapped += 1

    if swapped:
        log.debug("expanded %d results to parent content", swapped)
    return results


def stats() -> dict:
    return {
        "enabled": ENABLED,
        "cache_size": len(_CACHE),
        "cache_ttl_s": _CACHE_TTL_S,
        "max_cache_size": _MAX_CACHE_SIZE,
    }
