"""brain_core/rrf.py — Reciprocal Rank Fusion (Cormack, Clarke, Buettcher 2009).

Replaces the current "weighted score addition" approach in search_unified with
a rank-based fusion that's provably more robust to score-scale differences
across sources (ChromaDB vector vs canonical keyword vs Obsidian token-overlap).

RRF formula:
    score(doc) = Σ_i 1 / (k + rank_i(doc))

where rank_i is 0-indexed rank in source i, and k is a smoothing constant
(60 is the value from the original paper).

This module supports an optional `trust_weights` multiplier so canonical
still outranks obsidian on a tie, matching the existing `SOURCE_TRUST` table.

Usage:
    from rrf import rrf_fuse
    merged = rrf_fuse(
        result_lists=[rag_results, canonical_results, obsidian_results],
        trust_weights=[1.0, 1.0, 0.6],
        id_key="path",
    )
"""

from __future__ import annotations

from typing import Any

DEFAULT_K = 60


def rrf_fuse(
    result_lists: list[list[dict[str, Any]]],
    trust_weights: list[float] | None = None,
    id_key: str = "path",
    k: int = DEFAULT_K,
) -> list[dict[str, Any]]:
    """Fuse multiple ranked result lists via Reciprocal Rank Fusion.

    result_lists : list of ranked result lists (each list already sorted by
                   relevance descending)
    trust_weights: optional per-source trust multiplier (same length as
                   result_lists). Missing → all 1.0.
    id_key       : field name used to identify the same document across lists.
                   Defaults to "path" which matches brain's normalized results.
    k            : smoothing constant from the RRF paper (default 60).

    Returns a single fused list sorted desc by rrf_score. Each result dict
    gets a `rrf_score` field added so the caller can sort/filter on it.
    """
    if not result_lists:
        return []
    if trust_weights is None:
        trust_weights = [1.0] * len(result_lists)
    if len(trust_weights) != len(result_lists):
        raise ValueError(
            f"trust_weights length {len(trust_weights)} != result_lists length {len(result_lists)}"
        )

    rrf_scores: dict[str, float] = {}
    best_record: dict[str, dict[str, Any]] = {}

    for results, trust in zip(result_lists, trust_weights, strict=False):
        for rank, doc in enumerate(results):
            doc_id = doc.get(id_key) or doc.get("id") or doc.get("title")
            if not doc_id:
                # Anonymous doc — key by a stable hash of the content so the
                # same document from two sources fuses. id(doc) is Python's
                # memory address and differs per-process-per-call, which
                # defeats fusion entirely for anonymous results.
                import hashlib

                body = str(doc.get("content", ""))[:512]
                # md5 is fine here — non-crypto stable hash for dedup keying.
                doc_id = f"__anon_{hashlib.md5(body.encode(), usedforsecurity=False).hexdigest()[:12]}"
            doc_id = str(doc_id)
            contribution = trust * (1.0 / (k + rank))
            rrf_scores[doc_id] = rrf_scores.get(doc_id, 0.0) + contribution
            # Keep the highest-ranked version of the doc (usually the richest metadata).
            if doc_id not in best_record:
                best_record[doc_id] = doc

    # Normalize to 0..100 using the theoretical maximum so multi-source
    # consensus is preserved.  The theoretical max is achieved when a doc
    # appears at rank 0 in every source: sum(trust_i / k) for all sources.
    theoretical_max = sum(trust_weights) / k
    if theoretical_max <= 0:
        theoretical_max = 1.0

    fused: list[dict[str, Any]] = []
    for doc_id, raw in rrf_scores.items():
        # Shallow copy to avoid mutating the caller's original dicts.
        doc = dict(best_record[doc_id])
        normalized = max(0.0, min(100.0, (raw / theoretical_max) * 100.0))
        doc["rrf_score"] = round(normalized, 2)
        doc["score"] = doc["rrf_score"]
        # Score is now rank-based (RRF), not the upstream decayed/composed
        # value. Clearing _decay_applied lets the downstream time_decay pass
        # re-apply freshness against the new score; without this, expand/hyde
        # multi-variant queries effectively skipped time decay because the
        # idempotency flag from per-variant decay survived the shallow copy.
        doc.pop("_decay_applied", None)
        fused.append(doc)

    fused.sort(key=lambda d: d.get("rrf_score", 0), reverse=True)
    return fused


if __name__ == "__main__":
    # Smoke test — same doc appearing in multiple sources should outrank
    # a high-scoring doc that only appears in one.
    rag = [
        {"path": "/a", "title": "A", "score": 90},
        {"path": "/b", "title": "B", "score": 50},
        {"path": "/c", "title": "C", "score": 30},
    ]
    canon = [
        {"path": "/b", "title": "B (canonical)", "score": 80},
        {"path": "/d", "title": "D", "score": 60},
    ]
    obs = [
        {"path": "/b", "title": "B (obsidian)", "score": 70},
        {"path": "/e", "title": "E", "score": 40},
    ]
    fused = rrf_fuse([rag, canon, obs], trust_weights=[1.0, 1.0, 0.6])
    for r in fused:
        print(f"  [{r['rrf_score']:5.1f}] {r['path']}  {r['title']}")
