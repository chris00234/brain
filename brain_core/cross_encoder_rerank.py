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
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

log = logging.getLogger("brain.cross_encoder_rerank")

try:
    from config import BRAIN_CROSS_ENCODER_ENABLED
except ImportError:
    BRAIN_CROSS_ENCODER_ENABLED = False

try:
    from brain_core.source_quality import source_quality_multiplier
except ImportError:  # pragma: no cover - top-level import in scripts/tests
    from source_quality import source_quality_multiplier


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        log.warning("invalid %s=%r; using default %s", name, raw, default)
        return default


def _reranker_mode() -> str:
    mode = os.environ.get("BRAIN_RERANKER_MODE", "inprocess").strip().lower()
    if mode not in {"inprocess", "worker"}:
        log.warning("invalid BRAIN_RERANKER_MODE=%r; using inprocess", mode)
        return "inprocess"
    return mode


def _score_pairs(query: str, docs: list[str]) -> list[float]:
    if _reranker_mode() == "worker":
        try:
            from brain_core.reranker_client import score_pairs_remote
        except Exception as exc:
            log.warning("reranker client unavailable, skipping cross-encoder rerank: %s", exc)
            return []
        try:
            return score_pairs_remote(query, docs)
        except Exception as exc:
            log.warning("reranker worker unavailable, stage-1 result stands: %s", exc)
            return []

    # Lazy-import the model so tests / non-/recall paths don't pay the load cost.
    try:
        from brain_core.cross_encoder_model import score_pairs
    except Exception as e:
        try:
            from cross_encoder_model import score_pairs
        except Exception:
            log.warning("cross_encoder_model unavailable, skipping rerank: %s", e)
            return []
    return score_pairs(query, docs)


_BROAD_QUERY_RE = (
    "compare",
    "contrast",
    "summarize",
    "summary",
    "everything",
    "overall",
    "relationship",
    "tradeoff",
    "multi",
    "비교",
    "요약",
    "전체",
    "관계",
)


def choose_cross_encoder_top_k(query: str, results: list[dict], default_top_k: int = 14) -> int:
    """Choose the cross-encoder rerank window for this request.

    The cross-encoder is the hottest remaining recall phase. Most simple
    lookups don't need a 14-row semantic rerank window, while broad/multi-hop
    questions do. Keep this env-controlled so rollout can tune latency without
    changing retrieval semantics elsewhere.
    """
    max_top_k = _env_int("BRAIN_CROSS_ENCODER_TOP_K", default_top_k)
    if not _env_bool("BRAIN_CROSS_ENCODER_ADAPTIVE", False):
        return min(max_top_k, len(results))

    simple_top_k = _env_int("BRAIN_CROSS_ENCODER_SIMPLE_TOP_K", min(10, max_top_k))
    query_l = (query or "").lower()
    if any(marker in query_l for marker in _BROAD_QUERY_RE):
        return min(max_top_k, len(results))

    if len(results) >= 2:
        try:
            top = float(results[0].get("score", 0.0))
            second = float(results[1].get("score", 0.0))
            # If stage-1 already has a very strong canonical/current-truth
            # winner, rerank fewer tail rows. We do not skip CE entirely.
            source = str(results[0].get("source_type") or results[0].get("collection") or "").lower()
            if source in {"canonical", "semantic_memory"} and top >= 95.0 and (top - second) >= 18.0:
                return min(simple_top_k, len(results))
        except (TypeError, ValueError):
            pass
    return min(max(simple_top_k, 1), len(results))


def _sigmoid(x: float) -> float:
    """Clamp logits to [0, 1]. BGE-reranker-base outputs span ~[-10, 10]."""
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


def _normalize_model_scores(scores: list[float]) -> list[float]:
    """Normalize cross-encoder outputs to [0, 1].

    Some local BGE snapshots return probability-like scores in [0, 1], while
    older docs/examples describe logit-like outputs. Applying sigmoid to
    probability-like scores collapses the whole batch around 0.5 and erases the
    reranker signal. For [0, 1] outputs, use sqrt(score): it preserves ordering,
    keeps true zero at zero, and spreads low-but-meaningful positive relevance.
    """

    if not scores:
        return []
    finite_scores = [s for s in scores if math.isfinite(s)]
    if finite_scores and min(finite_scores) >= 0.0 and max(finite_scores) <= 1.0:
        return [math.sqrt(max(0.0, min(1.0, s))) if math.isfinite(s) else 0.0 for s in scores]
    return [_sigmoid(s) for s in scores]


def rerank_with_cross_encoder(query: str, results: list[dict], top_k: int = 20) -> list[dict]:
    """Rerank top-k results with a real cross-encoder. No-op if flag off.

    Computes CE score for each (query, doc) pair in one batched .predict() call,
    blends 20% original RRF/hybrid score + 80% CE score (normalized to 0-100),
    re-sorts the top_k window, leaves the tail unchanged.

    Every reranked row gets:
      - `cross_encoder_score` (sigmoid-normalized to [0, 1])
      - `ce_blended_score` (20/80 blend of original score x CE, in the same
                            scale as upstream RRF/hybrid scores)
      - `score` overwritten with ce_blended_score so downstream sorts work
    """
    if not BRAIN_CROSS_ENCODER_ENABLED or not results:
        return results

    subset = results[:top_k]
    tail = results[top_k:]

    # Build (query, title+content) pairs. Content capped at 1500 chars in score_pairs.
    docs = [((r.get("title") or "") + "\n" + (r.get("content") or "")) for r in subset]
    raw_scores = _score_pairs(query, docs)

    if not raw_scores or all(s == 0.0 for s in raw_scores):
        # Model failed entirely — return unchanged so the caller can fall back.
        return results

    # Median-fill: docs where the model returned exactly 0 (our sentinel for
    # per-doc failure inside score_pairs) get the median of successful scores
    # so they aren't silently advantaged by their original RRF rank.
    valid = [s for s in raw_scores if s != 0.0]
    median = sorted(valid)[len(valid) // 2] if valid else 0.0
    filled_scores = [s if s != 0.0 else median for s in raw_scores]

    # Normalize to [0, 1] while preserving probability-like model outputs.
    ce_normalized = _normalize_model_scores(filled_scores)

    # Blend 20% original x 80% CE (CE x 100 to match RRF score scale)
    for r, ce_norm in zip(subset, ce_normalized, strict=False):
        original = float(r.get("score", 0))
        blended = original * 0.2 + ce_norm * 100 * 0.8
        quality_mult = source_quality_multiplier(r, stage="cross_encoder")
        if quality_mult != 1.0:
            blended *= quality_mult
            debug = dict(r.get("_debug") or {})
            debug["source_quality_multiplier_cross_encoder"] = quality_mult
            r["_debug"] = debug
        r["cross_encoder_score"] = round(ce_norm, 4)
        r["ce_blended_score"] = round(blended, 2)
        r["score"] = r["ce_blended_score"]

    # Re-sort by blended score
    subset.sort(key=lambda r: r.get("ce_blended_score", 0), reverse=True)

    return subset + tail
