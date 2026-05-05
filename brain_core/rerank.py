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

import re
from typing import Any

try:
    from brain_core.source_quality import source_quality_multiplier
except ImportError:  # pragma: no cover - top-level import in scripts/tests
    from source_quality import source_quality_multiplier
from tokenizer import tokenize as _tokenize

_CONCRETE_INFRA_QUERY = re.compile(
    r"\b(?:nginx|docker(?:-compose)?|compose|server\s+block|default\s+server|"
    r"conf(?:ig)?|proxy_pass|upstream|listen|port|env(?:ironment)?|"
    r"credentials?|password|admin\s+password|couchdb|postgres|redis|minio|"
    r"watchtower|uptime[-\s]?kuma|loki)\b",
    re.I,
)
_CODE_QUERY = re.compile(
    r"\b(?:function|class|method|module|import|traceback|stacktrace|exception|"
    r"api\s+endpoint|return\s+value|parameter|async\s+def|__init__\.py)\b",
    re.I,
)
_CANONICAL_PROVENANCE_QUERY = re.compile(
    r"\b(?:canonical|workflow|conventional\s+commits?|git\s+workflow|"
    r"self[-\s]?learning|memory\s+extraction|search\s+pipeline|retrieval\s+pipeline|"
    r"pipeline\s+structure|brain\s+system)\b|(?:검색.*(?:파이프라인|구조)|파이프라인.*구조)",
    re.I,
)
_PRIMARY_FILE_TYPES = {
    "docker-compose",
    "docker_compose",
    "nginx-conf",
    "nginx_conf",
    "agent-config",
    "agent_config",
    "config",
    "yaml",
    "json",
    "toml",
}


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list | tuple | set):
        return " ".join(_as_text(v) for v in value)
    if isinstance(value, dict):
        return " ".join(f"{k} {_as_text(v)}" for k, v in value.items())
    return str(value)


def _metadata_haystack(result: dict[str, Any]) -> str:
    meta = result.get("metadata") or {}
    fields = [
        result.get("title", ""),
        result.get("path", ""),
        result.get("collection", ""),
        result.get("source_type", ""),
    ]
    for key in (
        "source",
        "source_path",
        "source_name",
        "document_title",
        "document_section",
        "document_type",
        "source_type",
        "type",
        "service",
        "agent",
        "tags",
        "context_tags",
        "source_aliases",
        "sources",
        "supersedes",
        "relations",
        "topic_key",
    ):
        fields.append(meta.get(key, ""))
    return " ".join(_as_text(v) for v in fields if v not in (None, "", [], {}))


def _metadata_overlap_boost(
    query: str, q_tokens: set[str], result: dict[str, Any]
) -> tuple[float, dict[str, Any]]:
    """Boost exact source/provenance matches without polluting dense embeddings.

    Dense vector similarity remains the main semantic signal. This tie-breaker
    lets path/title/tags/source aliases recover exact lookups like
    ``nginx default server block`` → ``default.conf`` and canonical provenance
    lookups after source-aware chunking shortened canonical note bodies.
    """

    if not q_tokens:
        return 1.0, {}

    meta_text = _metadata_haystack(result)
    meta_tokens = _tokenize(meta_text)
    overlap = len(q_tokens & meta_tokens) / max(len(q_tokens), 1)
    boost = 1.0
    debug: dict[str, Any] = {}
    if overlap:
        # Capped so metadata can break ties but cannot make irrelevant content win.
        meta_boost = min(1.35, 1.0 + (0.45 * overlap))
        boost *= meta_boost
        debug["metadata_overlap"] = round(overlap, 3)
        debug["metadata_overlap_boost"] = round(meta_boost, 3)

    query_text = query or " ".join(sorted(q_tokens))
    collection = str(result.get("collection") or "")
    meta = result.get("metadata") or {}
    path = str(result.get("path") or meta.get("source_path") or meta.get("source") or "").lower()
    dtype = str(meta.get("type") or meta.get("source_type") or meta.get("document_type") or "").lower()

    is_code_intent = bool(_CODE_QUERY.search(query_text))
    is_infra_intent = bool(_CONCRETE_INFRA_QUERY.search(query_text))
    is_primary_file = dtype in _PRIMARY_FILE_TYPES or any(
        marker in path
        for marker in ("/conf.d/", "docker-compose.yml", ".env", ".yaml", ".yml", ".json", ".toml")
    )
    if is_infra_intent:
        if collection == "knowledge" and is_primary_file:
            boost *= 1.45
            debug["infra_primary_source_boost"] = 1.45
        elif collection == "knowledge":
            boost *= 1.15
            debug["infra_knowledge_boost"] = 1.15
        if collection == "code" and not is_code_intent:
            boost *= 0.68
            debug["non_code_infra_code_penalty"] = 0.68
        if collection == "experience":
            boost *= 0.88
            debug["infra_experience_penalty"] = 0.88

    if _CANONICAL_PROVENANCE_QUERY.search(query_text):
        if collection in {"canonical", "distilled", "canonical_raptor"} or result.get("trust_tier", 0) >= 2:
            boost *= 1.35
            debug["canonical_provenance_boost"] = 1.35
        if collection == "code" and not is_code_intent:
            boost *= 0.8
            debug["canonical_query_code_penalty"] = 0.8

    return boost, debug


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
    metadata_boost, metadata_debug = _metadata_overlap_boost(query, q_tokens, result)
    # Removed 2026-04-12: "## Statement" penalty was hitting EVERY canonical note
    # (standard heading) not just derivative proposals, causing 5 of 7 eval
    # regressions to be canonical misses. Canonical trust_boost now carries this.

    reranked = base * relevance * pos_mult * trust_boost * semantic_boost * source_boost * metadata_boost

    if debug:
        result.setdefault("_debug", {}).update(
            {
                "rerank_base": base,
                "rerank_title_overlap": round(title_overlap, 3),
                "rerank_body_overlap": round(body_overlap, 3),
                "rerank_first_pos": first_pos,
                "rerank_pos_mult": round(pos_mult, 3),
                "rerank_relevance": round(relevance, 3),
                "rerank_metadata_boost": round(metadata_boost, 3),
                "rerank_score": round(reranked, 2),
                **metadata_debug,
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


def diversify_sources(
    results: list[dict[str, Any]],
    *,
    top_window: int = 5,
    max_per_source: int = 2,
    max_per_collection: int | None = None,
) -> list[dict[str, Any]]:
    """Reorder near-final results to prevent one source from crowding top-k.

    This is conservative: it never drops results and only moves overflow items
    behind the first result from a different source/collection. Useful after
    re-chunking, where a single file/session can produce many similar chunks.
    """

    if not results or top_window <= 1:
        return results
    selected: list[dict[str, Any]] = []
    overflow: list[dict[str, Any]] = []
    source_counts: dict[str, int] = {}
    collection_counts: dict[str, int] = {}
    for r in results:
        meta = r.get("metadata") or {}
        source = (
            r.get("path")
            or meta.get("source_path")
            or meta.get("source")
            or meta.get("document_id")
            or r.get("id")
            or ""
        )
        collection = str(r.get("collection") or "")
        source_counts[source] = source_counts.get(source, 0) + 1
        collection_counts[collection] = collection_counts.get(collection, 0) + 1
        source_over = bool(source) and source_counts[source] > max_per_source
        collection_over = (
            max_per_collection is not None
            and bool(collection)
            and collection_counts[collection] > max_per_collection
            and len(selected) < top_window
        )
        if len(selected) < top_window and (source_over or collection_over):
            overflow.append(r)
        else:
            selected.append(r)
    return selected + overflow


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
