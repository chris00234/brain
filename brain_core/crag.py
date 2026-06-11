"""brain_core/crag.py — Corrective RAG (CRAG) iterative retrieval scaffold.

Phase M9. CRAG (arXiv:2401.15884) is the simplest fix for the "single-shot
retrieval is the architectural ceiling" problem. The pattern:

    1. Run normal retrieval
    2. Score the result set's confidence
    3. If confidence is low, expand the query and retry
    4. Cap at max_hops=2 to bound latency

The brain already has a strong stage-1 retrieval; CRAG is purely a
gate that says "this answer is too uncertain, try a wider query."
For high-confidence queries (the common case), CRAG is a no-op except
for the score computation.

Confidence is derived from observable score statistics — no extra LLM call,
no separate classifier — so the gate stays sub-millisecond.

Wire-up: `server.py:_recall_v2` accepts an `iterative=True` opt-in param.
When set, the handler runs CRAG instead of plain recall. Default off so
existing callers see no behavior change.
"""

from __future__ import annotations

import atexit
import concurrent.futures
import logging
import os
import re
import statistics
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("brain.crag")


# 2026-04-17: previously a fresh ThreadPoolExecutor was created per
# expand_query() call and shut down with wait=False, leaving worker
# threads running when the next call created another executor. Under
# concurrent /recall/v2 traffic threads accumulated. Single shared
# module-level pool reuses the same two workers across all calls.
_expand_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="crag_expand")
atexit.register(_expand_pool.shutdown, wait=False)


# Confidence thresholds — calibrated against actual production score distributions
# (high-quality query: top score ~110, ce ~0.70, spread ~40
#  gibberish query: top score ~53, ce all 0.5, spread ~3)
HIGH_CONFIDENCE_TOP_SCORE = 80.0
LOW_CONFIDENCE_TOP_SCORE = 60.0
HIGH_CONFIDENCE_SPREAD = 25.0
LOW_CONFIDENCE_SPREAD = 8.0
CE_MEDIAN_FILL_VALUE = 0.5  # cross_encoder_rerank median-fill sentinel
MIN_RESULTS_FOR_CONFIDENCE = 3

# Iteration knobs
DEFAULT_MAX_HOPS = 2
DEFAULT_ITERATE_THRESHOLD = 0.30  # M9.3: calibrated against 150-query extended
# histogram (p10=0.327, current 0.45 triggered on 14.7% of queries). 0.30 gives
# ~7% trigger rate, keeping weighted p50 latency increase under 100ms when
# adaptive_rag also classifies the query as MULTI. adaptive_rag remains the
# primary gate — CRAG fires only when caller_explicit OR classification is MULTI.
DEFAULT_EXPANSION_TIMEOUT_S = 6.0  # hard wall-clock cap on the LLM dispatch
EXPANSION_PER_ATTEMPT_TIMEOUT_S = 8  # passed to cli_llm per-attempt


@dataclass(frozen=True)
class ConfidenceReport:
    """A score + the signals that contributed to it.

    Score is in [0, 1] where 1 = high confidence in the result set,
    0 = no useful signal. The components fields let callers debug and
    let observability dashboards plot the breakdown.
    """

    score: float
    n_results: int
    top_score: float
    score_spread: float
    ce_signal_present: bool
    ce_variance: float
    components: dict[str, float]


def score_confidence(results: list[dict], query: str | None = None) -> ConfidenceReport:
    """Compute a 0-1 confidence score from a recall result set.

    Signals (each contributes a partial score):
      - top_score: how strong is the #1 hit
      - score_spread: gap between #1 and the bottom of the window
      - ce_signal_present: did the cross-encoder return non-default values
      - ce_variance: are the CE scores actually distinguishing results
      - n_results: did we get enough results to even decide
      - optional query_coverage: do returned snippets mention the query

    The base score is still statistics-driven. When ``query`` is supplied, a
    lightweight lexical coverage penalty catches confident-looking but unrelated
    windows (for example impossible-service or off-domain questions) without an
    extra LLM call.
    Production tuning would replace this with a small classifier trained
    on (query, results, was_useful) tuples from /recall/feedback.
    """
    n = len(results)
    if n == 0:
        return ConfidenceReport(
            score=0.0,
            n_results=0,
            top_score=0.0,
            score_spread=0.0,
            ce_signal_present=False,
            ce_variance=0.0,
            components={"empty": 0.0},
        )

    raw_scores = [float(r.get("score", 0)) for r in results]
    top_score = max(raw_scores)
    bottom_score = min(raw_scores)
    score_spread = top_score - bottom_score

    ce_scores = [float(r.get("cross_encoder_score", CE_MEDIAN_FILL_VALUE)) for r in results]
    # CE signal is "present" when at least one score deviates from the median fill.
    ce_signal_present = any(abs(s - CE_MEDIAN_FILL_VALUE) > 0.05 for s in ce_scores)
    try:
        ce_variance = statistics.pvariance(ce_scores) if len(ce_scores) >= 2 else 0.0
    except statistics.StatisticsError:
        ce_variance = 0.0

    # Partial confidence components (each in [0, 1])
    c_top = _norm_top_score(top_score)
    c_spread = _norm_spread(score_spread)
    c_ce_present = 1.0 if ce_signal_present else 0.0
    c_ce_var = min(1.0, ce_variance * 50.0)  # variance ~0.02 → 1.0
    c_n = min(1.0, n / MIN_RESULTS_FOR_CONFIDENCE)

    components = {
        "top_score": c_top,
        "score_spread": c_spread,
        "ce_signal": c_ce_present,
        "ce_variance": c_ce_var,
        "n_results": c_n,
    }
    score = sum(components.values()) / len(components)
    coverage = _query_coverage(query, results)
    if coverage is not None:
        components["query_coverage"] = coverage
        if coverage < 0.34:
            score *= 0.30
        elif coverage < 0.50:
            score *= 0.60
        elif coverage < 0.80 and not ce_signal_present and c_spread <= 0.0:
            # High raw vector scores with no CE signal, no separating margin, and
            # a missed query term are often off-target windows that happen to
            # match generic words like "schedule" or "config". Keep complete or
            # well-separated windows untouched, but force this ambiguous class
            # through CRAG correction instead of silently accepting it.
            score *= 0.70

    return ConfidenceReport(
        score=round(score, 4),
        n_results=n,
        top_score=round(top_score, 2),
        score_spread=round(score_spread, 2),
        ce_signal_present=ce_signal_present,
        ce_variance=round(ce_variance, 4),
        components={k: round(v, 4) for k, v in components.items()},
    )


def _norm_top_score(top: float) -> float:
    """Map raw top score (0 to 110+) to a 0-1 confidence component."""
    if top >= HIGH_CONFIDENCE_TOP_SCORE:
        return 1.0
    if top <= LOW_CONFIDENCE_TOP_SCORE:
        return 0.0
    return (top - LOW_CONFIDENCE_TOP_SCORE) / (HIGH_CONFIDENCE_TOP_SCORE - LOW_CONFIDENCE_TOP_SCORE)


def _norm_spread(spread: float) -> float:
    """Map raw score spread (0 to 80+) to a 0-1 confidence component."""
    if spread >= HIGH_CONFIDENCE_SPREAD:
        return 1.0
    if spread <= LOW_CONFIDENCE_SPREAD:
        return 0.0
    return (spread - LOW_CONFIDENCE_SPREAD) / (HIGH_CONFIDENCE_SPREAD - LOW_CONFIDENCE_SPREAD)


def _query_coverage(query: str | None, results: list[dict]) -> float | None:
    """Return the fraction of meaningful query tokens found in result text.

    This is intentionally small and deterministic. It is not a relevance model;
    it is a guardrail for high-score windows that do not mention the actual ask.
    """
    if not query:
        return None
    stop = {
        "the",
        "and",
        "for",
        "with",
        "from",
        "that",
        "this",
        "what",
        "when",
        "where",
        "which",
        "who",
        "how",
        "does",
        "did",
        "are",
        "was",
        "were",
        "tomorrow",
        "yesterday",
        "about",
        "into",
    }
    tokens = [t for t in re.findall(r"[a-zA-Z0-9가-힣]+", query.lower()) if len(t) >= 3 and t not in stop]
    if len(tokens) < 2:
        return None
    haystack = " ".join(
        " ".join(
            str(part)
            for part in (
                r.get("title"),
                r.get("content"),
                r.get("path"),
                r.get("source"),
                r.get("collection"),
                r.get("source_type"),
            )
            if part
        )
        for r in results
        if isinstance(r, dict)
    ).lower()
    if not haystack:
        return 0.0
    covered = sum(1 for token in tokens if token in haystack)
    return round(covered / len(tokens), 4)


# Rewrite bridges are DATA, not code (2026-06-09): corpus-specific source
# vocabulary lives in brain_core/crag_rewrites.yaml so this module stays a
# generic mechanism. Mtime-cached, fail-open to no bridges — the generic
# script-split and LLM rewrite paths still run when the file is absent/broken.
_REWRITE_BRIDGES_PATH = Path(__file__).resolve().parent / "crag_rewrites.yaml"
_bridges_cache: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] | None = None
_bridges_mtime: float = -1.0


def _load_rewrite_bridges(
    path: Path | None = None,
) -> tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]:
    """Load (when_terms, rewrites) bridges from YAML. Fail-open to ()."""
    global _bridges_cache, _bridges_mtime
    bridge_path = path or _REWRITE_BRIDGES_PATH
    use_cache = path is None
    try:
        mtime = bridge_path.stat().st_mtime
    except OSError:
        return ()
    if use_cache and _bridges_cache is not None and mtime <= _bridges_mtime:
        return _bridges_cache
    try:
        import yaml

        with bridge_path.open() as f:
            raw = yaml.safe_load(f) or {}
        bridges: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        for entry in raw.get("bridges") or []:
            if not isinstance(entry, dict):
                continue
            when_terms = tuple(str(t).strip() for t in (entry.get("when_terms") or []) if str(t).strip())
            rewrites = tuple(str(r).strip() for r in (entry.get("rewrites") or []) if str(r).strip())
            if when_terms and rewrites:
                bridges.append((when_terms, rewrites))
        loaded = tuple(bridges)
    except Exception as exc:
        log.warning("crag rewrite bridges load failed (%s) — bridges disabled", exc)
        return ()
    if use_cache:
        _bridges_cache = loaded
        _bridges_mtime = mtime
    return loaded


def rule_based_rewrite_candidates(query: str) -> list[str]:
    """Small deterministic source-term rewrites for zero/weak-result queries.

    Two generic mechanisms: (1) mixed-script queries split into per-script
    variants, (2) data-driven source-term bridges from crag_rewrites.yaml.
    """
    if not query or not query.strip():
        return []
    lowered = query.lower()
    candidates: list[str] = []

    english_words = re.findall(r"[a-zA-Z][a-zA-Z0-9_.-]+", query)
    korean_parts = re.findall(r"[가-힣]+", query)
    if english_words and korean_parts:
        candidates.extend([" ".join(korean_parts), " ".join(english_words)])

    for needles, rewrites in _load_rewrite_bridges():
        if all(needle.lower() in lowered for needle in needles):
            candidates.extend(rewrites)

    out: list[str] = []
    seen = {query.strip().lower()}
    for candidate in candidates:
        normalized = " ".join(str(candidate or "").split())
        key = normalized.lower()
        if normalized and key not in seen:
            out.append(normalized)
            seen.add(key)
    return out[:4]


def should_iterate(report: ConfidenceReport, threshold: float = DEFAULT_ITERATE_THRESHOLD) -> bool:
    """True if confidence is below threshold AND we have at least 1 result.

    Empty result sets aren't a confidence problem — they're a recall problem
    that re-querying with an expanded query won't fix (the expansion still
    needs results to reason from). Empty → return False so the caller can
    fall through to a different recovery path.
    """
    if report.n_results == 0:
        return False
    return report.score < threshold


def _llm_rewrite_candidate(
    query: str,
    weak_results: list[dict],
    *,
    timeout_s: float,
    dispatch_fn: Callable[[str, str], str] | None = None,
) -> str | None:
    """Best-effort single LLM rewrite candidate."""
    context_lines = []
    for r in weak_results[:3]:
        title = (r.get("title") or "")[:100]
        snippet = (r.get("content") or "")[:200]
        if title or snippet:
            context_lines.append(f"- {title}: {snippet}")
    context = "\n".join(context_lines) if context_lines else "(no results)"

    prompt = (
        "The query below returned weak results. Rewrite it as a sparse keyword "
        "query for Chris's indexed personal/canonical knowledge. Preserve exact "
        "names, source terms, and non-English terms. If results are empty, avoid "
        "generic web-search phrases; prefer compact source vocabulary. Output ONLY "
        "the rewritten query, no explanation.\n\n"
        f"Original query: {query}\n\n"
        f"Top weak results:\n{context}\n\n"
        "Rewritten query:"
    )

    import concurrent.futures

    def _do_dispatch() -> str:
        if dispatch_fn is not None:
            return dispatch_fn("jenna", prompt) or ""
        from cli_llm import dispatch as _dispatch_real

        preferred_backend = os.getenv("BRAIN_CRAG_EXPAND_BACKEND") or None
        result = _dispatch_real(
            "jenna",
            prompt,
            thinking="off",
            timeout=EXPANSION_PER_ATTEMPT_TIMEOUT_S,
            backend=preferred_backend,
        )
        return result.text if result.ok else ""

    fut = _expand_pool.submit(_do_dispatch)
    try:
        raw = fut.result(timeout=timeout_s)
    except concurrent.futures.TimeoutError:
        log.warning("expand_query wall-clock budget exceeded (%.1fs)", timeout_s)
        raw = ""
    except Exception as _exc:
        log.warning("expand_query dispatch failed: %s", _exc)
        raw = ""
    if not raw:
        return None
    candidate = raw.strip().strip('"').strip("'").strip()
    candidate = candidate.splitlines()[0].strip()
    candidate = re.sub(r"^[-*\d\.)\s]+", "", candidate).strip()
    if len(candidate) > 500:
        candidate = candidate[:500]
    if candidate and candidate.lower() != query.lower():
        return candidate
    return None


def expand_query_candidates(
    query: str,
    weak_results: list[dict],
    *,
    timeout_s: float = DEFAULT_EXPANSION_TIMEOUT_S,
    dispatch_fn: Callable[[str, str], str] | None = None,
) -> list[dict[str, str]]:
    """Return ordered CRAG rewrite candidates from rules plus the live LLM path."""
    if not query or not query.strip():
        return []

    candidates: list[dict[str, str]] = []
    seen = {query.strip().lower()}

    def add(source: str, candidate: str | None) -> None:
        normalized = " ".join(str(candidate or "").split())
        key = normalized.lower()
        if normalized and key not in seen:
            candidates.append({"source": source, "query": normalized})
            seen.add(key)

    for candidate in rule_based_rewrite_candidates(query):
        add("rule", candidate)
    if candidates and os.getenv("BRAIN_CRAG_INCLUDE_LLM_AFTER_RULES", "0").lower() not in {
        "1",
        "true",
        "yes",
    }:
        return candidates[:5]
    add("llm", _llm_rewrite_candidate(query, weak_results, timeout_s=timeout_s, dispatch_fn=dispatch_fn))
    return candidates[:5]


def expand_query(
    query: str,
    weak_results: list[dict],
    *,
    timeout_s: float = DEFAULT_EXPANSION_TIMEOUT_S,
    dispatch_fn: Callable[[str, str], str] | None = None,
) -> str | None:
    """Generate one expanded query via deterministic source terms + CLI LLM."""
    rules = rule_based_rewrite_candidates(query)
    if rules:
        return rules[0]
    return _llm_rewrite_candidate(
        query,
        weak_results,
        timeout_s=timeout_s,
        dispatch_fn=dispatch_fn,
    )


def iterative_recall(
    query: str,
    recall_fn: Callable[[str], list[dict]],
    *,
    max_hops: int = DEFAULT_MAX_HOPS,
    threshold: float = DEFAULT_ITERATE_THRESHOLD,
    expand_fn: Callable[[str, list[dict]], str | None] | None = None,
) -> tuple[list[dict], dict]:
    """Run CRAG-style iterative retrieval.

    Args:
        query: the original user query
        recall_fn: a callable that takes a query string and returns recall results.
                   The caller wraps the brain's normal recall path so CRAG stays
                   testable in isolation.
        max_hops: max retrieval attempts (1 = no iteration, 2 = original + 1 retry)
        threshold: confidence below this triggers expansion
        expand_fn: query rewrite fn; defaults to expand_query

    Returns:
        (best_results, telemetry) where telemetry is a dict suitable for
        merging into the recall_v2 timing/explain payload:
            {
              "hops": int,
              "confidence_history": [score, ...],
              "expansions": [rewritten_query, ...],
              "final_confidence": float,
              "iterated": bool,
            }
    """
    expand_fn = expand_fn or expand_query
    confidence_history: list[float] = []
    expansions: list[str] = []
    current_query = query
    best_results: list[dict] = []
    best_score = -1.0
    iterated = False

    for hop in range(max(1, max_hops)):
        results = recall_fn(current_query)
        report = score_confidence(results, query=current_query)
        confidence_history.append(report.score)

        # Track the best window seen across hops so we don't degrade
        if report.score > best_score:
            best_results = results
            best_score = report.score

        if not should_iterate(report, threshold):
            # Confident enough — stop early
            break

        # Don't bother expanding on the last hop — no retry possible
        if hop + 1 >= max_hops:
            break

        rewritten = expand_fn(current_query, results)
        if not rewritten:
            # Expansion failed → stop, return best so far
            break
        expansions.append(rewritten)
        current_query = rewritten
        iterated = True

    telemetry = {
        "hops": len(confidence_history),
        "confidence_history": [round(s, 4) for s in confidence_history],
        "expansions": expansions,
        "final_confidence": round(best_score, 4) if best_score >= 0 else 0.0,
        "iterated": iterated,
    }
    return best_results, telemetry
