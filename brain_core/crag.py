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

import logging
import statistics
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger("brain.crag")


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
DEFAULT_ITERATE_THRESHOLD = 0.45  # score below this → trigger expansion
DEFAULT_EXPANSION_TIMEOUT_S = 6.0  # hard wall-clock cap on the LLM dispatch
EXPANSION_PER_ATTEMPT_TIMEOUT_S = 3  # passed to openclaw_dispatch per-attempt


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


def score_confidence(results: list[dict]) -> ConfidenceReport:
    """Compute a 0-1 confidence score from a recall result set.

    Signals (each contributes a partial score):
      - top_score: how strong is the #1 hit
      - score_spread: gap between #1 and the bottom of the window
      - ce_signal_present: did the cross-encoder return non-default values
      - ce_variance: are the CE scores actually distinguishing results
      - n_results: did we get enough results to even decide

    The five partial scores are averaged — no learned weights yet.
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


def expand_query(
    query: str,
    weak_results: list[dict],
    *,
    timeout_s: float = DEFAULT_EXPANSION_TIMEOUT_S,
    dispatch_fn: Callable[[str, str], str] | None = None,
) -> str | None:
    """Generate an expanded query via openclaw_dispatch (best-effort).

    Args:
        query: the original query
        weak_results: the low-confidence result set (top 3 used as context)
        timeout_s: hard cap on the LLM dispatch
        dispatch_fn: optional injection for tests; defaults to openclaw_dispatch.dispatch

    Returns:
        the expanded query string, or None if dispatch failed / timed out.
        Returning None signals the caller to fall through (no retry).
    """
    if not query or not query.strip():
        return None

    # Use the top 3 results as context for the rewrite. Truncate hard to
    # keep the prompt small + the LLM call cheap.
    context_lines = []
    for r in weak_results[:3]:
        title = (r.get("title") or "")[:100]
        snippet = (r.get("content") or "")[:200]
        if title or snippet:
            context_lines.append(f"- {title}: {snippet}")
    context = "\n".join(context_lines) if context_lines else "(no results)"

    prompt = (
        f"The query below returned weak results. Rewrite it as a more specific "
        f"or differently-worded query that might find better matches. Output ONLY "
        f"the rewritten query, no explanation.\n\n"
        f"Original query: {query}\n\n"
        f"Top weak results:\n{context}\n\n"
        f"Rewritten query:"
    )

    # Wrap the entire dispatch path in suppress so the recall hot path never
    # crashes on a transient openclaw / model error. The whole dispatch is
    # ALSO bounded by a wall-clock budget via concurrent.futures so callers
    # pay at most `timeout_s` even when openclaw_dispatch's internal retry
    # loop would otherwise fire 2-3 times for a slow model.
    import concurrent.futures

    rewritten: str | None = None

    def _do_dispatch() -> str:
        if dispatch_fn is not None:
            return dispatch_fn("jenna", prompt) or ""
        from openclaw_dispatch import dispatch as _dispatch_real

        result = _dispatch_real(
            "jenna",
            prompt,
            thinking="off",
            timeout=EXPANSION_PER_ATTEMPT_TIMEOUT_S,
        )
        return result.text if result.ok else ""

    # M7-WS7 M4 fix: do NOT use `with executor as ex:` — the context manager's
    # __exit__ blocks until in-flight work completes, which means a stuck Jenna
    # dispatch would freeze the entire /recall/v2 request even after the
    # wall-clock timeout fires. Use a manual executor and `shutdown(wait=False,
    # cancel_futures=True)` instead so timeout actually unblocks the caller.
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(_do_dispatch)
        try:
            raw = fut.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError:
            log.warning("expand_query wall-clock budget exceeded (%.1fs)", timeout_s)
            raw = ""
        except Exception as _exc:
            log.warning("expand_query dispatch failed: %s", _exc)
            raw = ""
        if raw:
            candidate = raw.strip().strip('"').strip("'").strip()
            if candidate and candidate != query:
                if len(candidate) > 500:
                    candidate = candidate[:500]
                rewritten = candidate
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return rewritten


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
        report = score_confidence(results)
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
