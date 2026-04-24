"""brain_core/rerank.py — token-overlap reranker (no LLM).

The dominant retrieval failure mode in the current search pipeline is:
results that score high on vector similarity but mismatch the query's
actual keywords end up in the top-5. A real cross-encoder would fix this
but would require hosting a model — violating Chris's "Ollama stays
embedder-only" rule.

This module is a pure-Python alternative that boosts relevance using:
  1. Title token overlap        (strongest signal — 2x weight)
  2. Body token overlap         (1x weight)
  3. Position of first match    (earlier = better)
  4. Length penalty             (shorter, denser matches preferred)

It generalizes the canonical-only penalty that already lived in
`search_unified.normalize_canonical_result` to every source.

Usage:
    from rerank import rerank
    reranked = rerank(query, results, top_k=10)
"""

from __future__ import annotations

from typing import Any

try:
    from brain_core.source_quality import source_quality_multiplier
except ImportError:  # pragma: no cover - top-level import in scripts/tests
    from source_quality import source_quality_multiplier
from tokenizer import tokenize as _tokenize


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _first_match_position(tokens: set[str], text: str) -> int:
    """Return the character index of the earliest query-token match in text."""
    if not tokens or not text:
        return -1
    lower = text.lower()
    positions: list[int] = []
    for t in tokens:
        idx = lower.find(t)
        if idx != -1:
            positions.append(idx)
    if not positions:
        return -1
    return min(positions)


def _position_decay(first_pos: int, body_len: int, gamma: float = 500.0) -> float:
    """Bonus for matches that appear early in the content.

    first_pos = -1           → 1.0  (no early-position bonus)
    first_pos = 0            → 1.5  (match at start)
    first_pos >> gamma        → ~1.0 (decays back to baseline)
    """
    if first_pos < 0:
        return 1.0
    return 1.0 + 0.5 * max(0.0, 1.0 - first_pos / gamma)


def score_result(query: str, result: dict[str, Any], debug: bool = False) -> float:
    """Compute a reranked score for a single result dict.

    Expects result to have at least: title, content, score. Other fields are
    passed through untouched.
    """
    q_tokens = _tokenize(query)
    if not q_tokens:
        return float(result.get("score", 0))

    title = result.get("title", "") or ""
    content = result.get("content", "") or ""
    base = float(result.get("score", 0))

    title_tokens = _tokenize(title)
    body_tokens = _tokenize(content[:1500])
    title_overlap = _jaccard(q_tokens, title_tokens)
    body_overlap = _jaccard(q_tokens, body_tokens)
    first_pos = _first_match_position(q_tokens, content[:1200])
    pos_mult = _position_decay(first_pos, 1200)

    # Relevance multiplier in [0.3, 2.5] range.
    relevance = 1.0 + (2.0 * title_overlap) + (1.0 * body_overlap)
    # Penalize near-zero overlap — softened for semantic matches.
    if title_overlap == 0 and body_overlap < 0.05:
        relevance *= 0.75 if result.get("trust_tier", 0) >= 2 else 0.6

    trust_tier = result.get("trust_tier", 1)
    if not isinstance(trust_tier, int | float):
        trust_tier = 1
    # Trust boost must be strong enough to counterbalance title-overlap wins.
    # A high-overlap experience hit gets relevance ~3.0x; canonical needs ~1.4x
    # trust to stay in top-5 against it. Bumped 2026-04-12 after eval regression
    # showed 5 canonical hits getting pushed out of top-5 by experience notes.
    trust_boost = {3: 1.4, 2: 1.15}.get(trust_tier, 1.0)

    # Use vector_score from Qdrant when available as semantic relevance signal
    vector_score = float(result.get("metadata", {}).get("vector_score", 0) or result.get("vector_score", 0))
    semantic_boost = 1.0 + (0.5 * vector_score) if vector_score > 0.65 else 1.0

    # Boost primary source files (config, agent definitions) over derivative canonical notes
    path = result.get("path", "")
    source_boost = 1.0
    if any(p in path for p in ("docker-compose.yml", "/conf.d/", "AGENTS.md", "TOOLS.md")):
        # Primary source files should beat derivative notes when the query is
        # explicitly asking about that service/agent/config file.
        path_tokens = _tokenize(path)
        source_boost = 1.6 if q_tokens & path_tokens else 1.2

    # Raw operational logs are fallback evidence, not primary answers. Keep the
    # quality policy shared with cross_encoder_rerank so CE score overwrites do
    # not erase the same source penalty later in the route.
    source_boost *= source_quality_multiplier(result, stage="lexical")
    # Removed 2026-04-12: "## Statement" penalty was hitting EVERY canonical note
    # (standard heading) not just derivative proposals, causing 5 of 7 eval
    # regressions to be canonical misses. Canonical trust_boost now carries this.

    reranked = base * relevance * pos_mult * trust_boost * semantic_boost * source_boost

    if debug:
        result.setdefault("_debug", {}).update(
            {
                "rerank_base": base,
                "rerank_title_overlap": round(title_overlap, 3),
                "rerank_body_overlap": round(body_overlap, 3),
                "rerank_first_pos": first_pos,
                "rerank_pos_mult": round(pos_mult, 3),
                "rerank_relevance": round(relevance, 3),
                "rerank_score": round(reranked, 2),
            }
        )

    return reranked


def rerank(
    query: str, results: list[dict[str, Any]], top_k: int | None = None, debug: bool = False
) -> list[dict[str, Any]]:
    """Rerank results in place by token-overlap score.

    Each result gets a `rerank_score` field added to its top level so the
    caller can inspect or sort on the reranked value. Returns a new list
    sorted desc by rerank_score, optionally truncated to top_k.
    Idempotent — if all results already carry `_rerank_applied`, skip the
    score recomputation and only re-sort + truncate.
    """
    if not results:
        return []

    scored = list(results)
    if not all(r.get("_rerank_applied") for r in scored):
        for r in scored:
            r["rerank_score"] = round(score_result(query, r, debug=debug), 2)
            r["_rerank_applied"] = True

    scored.sort(key=lambda r: r.get("rerank_score", 0), reverse=True)

    if top_k is not None and top_k > 0:
        scored = scored[:top_k]

    return scored


if __name__ == "__main__":
    # Smoke test
    test_results = [
        {
            "title": "OpenClaw gateway config",
            "content": "The openclaw gateway runs on port 18789",
            "score": 50,
        },
        {"title": "Random notes", "content": "Something about docker and nginx setups", "score": 80},
        {"title": "Gateway docs", "content": "openclaw gateway openclaw openclaw", "score": 30},
    ]
    rerank("openclaw gateway", test_results, debug=True)
