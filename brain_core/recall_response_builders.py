"""Pure response-building helpers for recall routes.

These functions used to live inline in ``routes.recall``. Keeping them here
separates response-shaping and cache-key contracts from endpoint orchestration
and retrieval governance.
"""

from __future__ import annotations

import time
from typing import Any

from recall_models import RecallV2Response


def _build_empty_recall_v2_response(
    q: str,
    *,
    hyde: bool,
    hypothetical: str | None,
    variants: list[str],
    expand: bool,
    rerank: bool,
    decay: bool,
    t_start: float,
    timing: dict[str, Any],
) -> RecallV2Response:
    """Build a no-results RecallV2Response with the metadata fields populated.

    Called when every payload returned empty results — the route still
    surfaces the query echo + flags + timing so the caller can see why
    nothing came back (e.g. timing["search_ms"]) without an empty
    `results` array hiding the upstream signal.

    `variants` is included only when expand=True so the caller can see
    which variants ran; otherwise the field stays empty to match the
    pre-extraction behavior exactly.
    """
    return RecallV2Response(
        query=q,
        results=[],
        total_candidates=0,
        hyde_used=hyde,
        hypothetical=hypothetical,
        variants=variants if expand else [],
        rerank_applied=rerank,
        time_decay_applied=decay,
        latency_ms=int((time.time() - t_start) * 1000),
        timing=timing,
    )


def _filter_nonempty_result_lists(payloads: list[dict]) -> list[list[dict]]:
    """Pull the `results` list out of each payload, dropping empty/missing.

    Each payload from `search_unified.search_all` either has a non-empty
    `results` list, an empty list, or is missing the key entirely. RRF
    fusion needs only the non-empty lists; the rest would distort the
    reciprocal-rank contributions if included as empty arrays.
    """
    return [p.get("results", []) for p in payloads if p.get("results")]


def _merge_source_timing(
    timing: dict[str, Any],
    payloads: list[dict],
) -> None:
    """Merge per-source timing keys from each search_all payload into `timing`.

    Each payload from `search_unified.search_all` carries a "source_timing"
    dict (rag_ms, canonical_ms, ...). When multiple variants run in parallel,
    we take the MAX across variants since sources run in parallel inside
    each search_all call — wall-clock for each source is the slowest one.

    Mutates `timing` in place; returns None.
    """
    for p in payloads:
        for k, v in p.get("source_timing", {}).items():
            timing[k] = max(timing.get(k, 0), v)


def _build_recall_v2_cache_key(
    request: Any,
    q: str,
    n: int,
    *,
    hyde: bool,
    expand: bool,
    rerank: bool,
    decay: bool,
    iterative: bool,
    collection: str | None,
    domain: str | None,
    agent: str | None,
    since: str | None,
    until: str | None,
    entity: str | None,
    source_type: str | None,
    include_history: bool,
    include_obsolete: bool,
    as_of: str | None,
    canonical_first: bool,
    exclude_already_used: bool,
) -> str:
    """Build the response-cache key for /recall/v2.

    Includes session_id + agent + active embedder adapter so concurrent
    sessions don't share each other's spreading-activation-boosted results
    (2026-04-16 R-3) and adapter swaps don't serve stale pre-adapter cached
    rows (2026-04-17 LoRA A/B fix).
    """
    sess_hdr = request.headers.get("x-session-id", "")
    agent_hdr = request.headers.get("x-agent", "")
    try:
        from indexer import _lora_embedder as _active_adapter

        adapter_marker = _active_adapter[0] if _active_adapter else "base"
    except Exception:
        adapter_marker = "base"
    return (
        f"{q}:{n}:{hyde}:{expand}:{rerank}:{decay}:{iterative}:{collection}:"
        f"{domain}:filter_agent={agent}:{since}:{until}:{entity}:{source_type}:"
        f"{include_history}:{include_obsolete}:{as_of}:{canonical_first}:"
        f"excl={exclude_already_used}:"
        f"sess={sess_hdr}:agent={agent_hdr}:emb={adapter_marker}"
    )


def _build_meta_note(top_results: list[dict]) -> str | None:
    """Compose a proactive metacognitive note when the top-1 result has
    signals of uncertainty. Heuristic only — no LLM call, fires in <1ms.

    Triggers (any):
      1. Calibrated confidence < 0.5 on top-1
      2. pending_contradictions > 0 on top-1
      3. Top-2 scores within 5% — ambiguous winner
      4. trust_tier == 0 on top-1 AND every other result <40 score

    Multiple triggers combine with " · " separator. Returns None when no
    trigger fires so high-confidence queries stay clean.
    """
    if not top_results:
        return None
    top1 = top_results[0] if isinstance(top_results[0], dict) else None
    if top1 is None:
        return None
    notes: list[str] = []

    # 1. Low calibrated confidence
    try:
        conf = float(top1.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    if conf and conf < 0.5:
        notes.append(f"⚠ Low confidence ({conf:.2f}) — verify before acting")

    # 2. Pending contradictions
    try:
        pc = int(top1.get("pending_contradictions") or 0)
    except (TypeError, ValueError):
        pc = 0
    if pc > 0:
        plural = "s" if pc > 1 else ""
        notes.append(f"⚠ Top result has {pc} open contradiction{plural} — call brain_doubt for both sides")

    # 3. Ambiguous top-2
    if len(top_results) >= 2 and isinstance(top_results[1], dict):
        try:
            s1 = float(top1.get("score") or 0)
            s2 = float(top_results[1].get("score") or 0)
            if s1 > 0 and (s1 - s2) / s1 < 0.05:
                notes.append(f"⚠ Ambiguous: top-2 scores within {((s1 - s2) / s1) * 100:.1f}%")
        except (TypeError, ValueError):
            pass

    # 4. Untrusted top-1 with weak alternatives
    try:
        top1_trust = int(top1.get("trust_tier") or 0)
        top1_score = float(top1.get("score") or 0)
    except (TypeError, ValueError):
        top1_trust, top1_score = 0, 0.0
    if top1_trust == 0 and top1_score > 40:
        others_weak = all(
            float((r or {}).get("score") or 0) < 40 for r in top_results[1:4] if isinstance(r, dict)
        )
        if others_weak:
            notes.append("⚠ No high-trust match — top result is untiered")

    if not notes:
        return None
    return " · ".join(notes)
