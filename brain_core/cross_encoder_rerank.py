"""brain_core/cross_encoder_rerank.py — Real cross-encoder reranker.

Uses sentence_transformers.CrossEncoder (BGE-reranker-base by default) to rerank
top-k retrieval results by query-document relevance. Much more accurate than
token-overlap for semantic matches; typical gain on retrieval eval: +3 to +8pt.

Feature-flagged via BRAIN_CROSS_ENCODER_ENABLED (set in brain-server plist).
When disabled, returns results unchanged so callers can fall back to token-overlap.

Replaced Ollama/qwen2.5 scorer 2026-04-12 — the LLM approach was serial HTTP
per doc and the real cross-encoder is batched, ~10x faster and measurably better.
"""

from __future__ import annotations

import logging
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

log = logging.getLogger("brain.cross_encoder_rerank")

try:
    from config import BRAIN_CROSS_ENCODER_ENABLED
except ImportError:
    BRAIN_CROSS_ENCODER_ENABLED = False


def _sigmoid(x: float) -> float:
    """Clamp logits to [0, 1]. BGE-reranker-base outputs span ~[-10, 10]."""
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


def rerank_with_cross_encoder(query: str, results: list[dict], top_k: int = 20) -> list[dict]:
    """Rerank top-k results with a real cross-encoder. No-op if flag off.

    Computes CE score for each (query, doc) pair in one batched .predict() call,
    blends 20% original RRF/hybrid score + 80% CE score (normalized to 0-100),
    re-sorts the top_k window, leaves the tail unchanged.

    Every reranked row gets:
      - `cross_encoder_score` (sigmoid-normalized to [0, 1])
      - `ce_blended_score` (20/80 blend of original score × CE, in the same
                            scale as upstream RRF/hybrid scores)
      - `score` overwritten with ce_blended_score so downstream sorts work
    """
    if not BRAIN_CROSS_ENCODER_ENABLED or not results:
        return results

    # Lazy-import the model so tests / non-/recall paths don't pay the load cost.
    try:
        from cross_encoder_model import score_pairs
    except Exception as e:
        log.warning("cross_encoder_model unavailable, skipping rerank: %s", e)
        return results

    subset = results[:top_k]
    tail = results[top_k:]

    # Build (query, title+content) pairs. Content capped at 1500 chars in score_pairs.
    docs = [((r.get("title") or "") + "\n" + (r.get("content") or "")) for r in subset]
    raw_scores = score_pairs(query, docs)

    if not raw_scores or all(s == 0.0 for s in raw_scores):
        # Model failed entirely — return unchanged so the caller can fall back.
        return results

    # Median-fill: docs where the model returned exactly 0 (our sentinel for
    # per-doc failure inside score_pairs) get the median of successful scores
    # so they aren't silently advantaged by their original RRF rank.
    valid = [s for s in raw_scores if s != 0.0]
    median = sorted(valid)[len(valid) // 2] if valid else 0.0
    filled_scores = [s if s != 0.0 else median for s in raw_scores]

    # Normalize to [0, 1] via sigmoid
    ce_normalized = [_sigmoid(s) for s in filled_scores]

    # Blend 20% original × 80% CE (CE × 100 to match RRF score scale)
    for r, ce_norm in zip(subset, ce_normalized, strict=False):
        original = float(r.get("score", 0))
        blended = original * 0.2 + ce_norm * 100 * 0.8
        r["cross_encoder_score"] = round(ce_norm, 4)
        r["ce_blended_score"] = round(blended, 2)
        r["score"] = r["ce_blended_score"]

    # Re-sort by blended score
    subset.sort(key=lambda r: r.get("ce_blended_score", 0), reverse=True)

    return subset + tail
