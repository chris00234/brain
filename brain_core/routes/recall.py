"""Recall suite: /recall, /recall/v2, /recall/stream, /recall/batch,
/recall/feedback, /recall/active.

Extracted from server.py as-is. Request/response schemas live in
recall_models; shared caches (_recall_cache, _recall_embedding_cache)
live in recall_cache — both re-exported here for legacy import sites.
Extensive imports reflect the original module surface.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import active_recall
import hyde as _hyde
import rerank as _rerank
import rrf as _rrf
import search_unified
import temporal
import time_decay as _time_decay
from api_deps import _safe_http_detail, get_request_id, log, verify_bearer
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from indexer import get_embedding as _get_embedding
from metrics_buffer import metrics_buffer as _metrics_buf
from rate_limit import limiter
from recall_cache import (
    _recall_cache as _recall_cache,
)
from recall_cache import (
    _recall_cache_get,
    _recall_cache_put,
)
from recall_cache import (
    _recall_emb_cache_lookup as _recall_emb_cache_lookup_base,
)
from recall_cache import (
    _recall_emb_cache_put as _recall_emb_cache_put_base,
)
from recall_cache import (
    _recall_embedding_cache as _recall_embedding_cache,
)
from recall_cache import (
    clear_caches as _clear_recall_caches,
)
from recall_models import (
    CompoundOp,
    CompoundRequest,
    InjectionBlockModel,
    RecallActiveRequest,
    RecallActiveResponse,
    RecallBatchRequest,
    RecallResponse,
    RecallResult,
    RecallResultMetadata,
    RecallV2Response,
    SearchFeedbackRequest,
)
from recall_response_builders import (
    _build_empty_recall_v2_response,
    _build_meta_note,
    _build_recall_v2_cache_key,
    _filter_nonempty_result_lists,
    _merge_source_timing,
)
from recall_temporal import _apply_temporal_filter_inplace

# Compatibility contract: recall_v2 cache keys include filter_agent={agent};
# see tests/unit/test_hermes_current_migration.py.

__all__ = [
    "CompoundOp",
    "CompoundRequest",
    "InjectionBlockModel",
    "RecallActiveRequest",
    "RecallActiveResponse",
    "RecallBatchRequest",
    "RecallResponse",
    "RecallResult",
    "RecallResultMetadata",
    "RecallV2Response",
    "SearchFeedbackRequest",
    "clear_caches",
]

# ── Shared recall-governance layer ────────────────────────────────────────
# Query analysis, source authority, and the multilingual tokenizer now live in
# brain_core/recall_governance/ so /recall/v2, /recall/active, and Hermes
# provider prefetch consume ONE contract instead of drifting copies. These
# module-level aliases preserve every existing `routes.recall._is_*` /
# `_result_*` import site; the topic-specific governance below (augment,
# governance-inplace, retrieval-quality filter) stays here and calls them.
from recall_governance import generic_queries as _generic_query_helpers
from recall_governance import quality as _quality_helpers
from recall_governance import query_analyzer as _query_analyzer
from recall_governance import source_authority as _source_authority
from recall_governance.normalization import (
    tokenize as _tokenize_recall_text,
)
from recall_governance.query_analyzer import (
    is_live_state_query as _is_live_state_query,
)
from recall_governance.query_analyzer import (
    is_out_of_domain_world_knowledge_query as _is_out_of_domain_world_knowledge_query,
)
from recall_governance.query_analyzer import (
    is_positive_summary_intent_query as _is_positive_summary_intent_query,
)
from recall_governance.query_analyzer import (
    is_summary_excluded_query as _is_summary_excluded_query,
)
from recall_governance.route_guarantees import (
    guarantee_tokens as _guarantee_tokens,
)
from recall_governance.route_guarantees import (
    match_route_guarantees as _match_route_guarantees,
)
from recall_governance.source_authority import (
    is_distilled_brain_analysis_result as _is_distilled_brain_analysis_result,
)
from recall_governance.source_authority import (
    is_durable_truth_result as _is_durable_truth_result,
)
from recall_governance.source_authority import (
    is_episodic_event_log_result as _is_episodic_event_log_result,
)
from recall_governance.source_authority import (
    is_generic_summary_result as _is_generic_summary_result,
)
from recall_governance.source_authority import (
    is_low_authority_result as _is_low_authority_result,
)
from recall_governance.source_authority import (
    is_query_keyed_bridge_result as _is_query_keyed_bridge_result,
)
from recall_governance.source_authority import (
    is_source_or_test_file_result as _is_source_or_test_file_result,
)
from recall_governance.source_authority import (
    is_vanished_source_result as _is_vanished_source_result,
)
from recall_governance.source_authority import (
    result_category as _result_category,
)
from recall_governance.source_authority import (
    result_metadata as _result_metadata,
)
from recall_governance.source_authority import (
    result_text as _result_text,
)
from vector_store import get_vector_store

from config import (
    BRAIN_DIR,
    KNOWLEDGE_DIR,
    OBSIDIAN_VAULT,
    OBSIDIAN_VAULT_ICLOUD,
    OBSIDIAN_VAULT_LOCAL,
    OPENCLAW_DIR,
)

# Constants kept under their original names for the topic-specific governance
# functions that still live in this module and for legacy compatibility aliases.
_TRUTH_CATEGORIES = _source_authority._TRUTH_CATEGORIES
_GENERIC_SUMMARY_MARKERS = _source_authority._GENERIC_SUMMARY_MARKERS
_GENERIC_PROCEDURE_STOPWORDS = _query_analyzer._GENERIC_PROCEDURE_STOPWORDS
_PERSONAL_MEMORY_TOKENS = _query_analyzer._PERSONAL_MEMORY_TOKENS
_WORLD_KNOWLEDGE_ANCHOR_TOKENS = _query_analyzer._WORLD_KNOWLEDGE_ANCHOR_TOKENS
_normalize_recall_signature = _quality_helpers.normalize_recall_signature
_near_duplicate_key = _quality_helpers.near_duplicate_key
_is_near_duplicate_signature = _quality_helpers.is_near_duplicate_signature
_quality_rank_tuple = _quality_helpers.quality_rank_tuple
_is_conversation_transcript_row = _quality_helpers.is_conversation_transcript_row
_GENERIC_RECIPE_QUERY_TOKENS = _generic_query_helpers.GENERIC_RECIPE_QUERY_TOKENS
_RECIPE_RESULT_TOKENS = _generic_query_helpers.RECIPE_RESULT_TOKENS
_is_generic_recipe_query = _generic_query_helpers.is_generic_recipe_query
_is_recipe_result = _generic_query_helpers.is_recipe_result
# Tool/media/runtime domain nouns whose durable answers use synonym-rich,
# NON-literal vocabulary (Apple Calendar / macOS for "calendar", GPT Images for
# "image", Codex-through-Hermes for "codex"). A query naming one of these is a
# tooling/constraint probe, so the strict whole-word personal_factoid overlap
# gate must NOT fire for it — the relevant row rarely repeats the prompt's
# literal term, and Korean prompts glue particles onto the noun. Mirrors the
# Hermes provider's _CONSTRAINT_QUERY_RE domain nouns. Deliberately EXCLUDES
# abstract attribute nouns (cost/spend/ai/model/local) that ARE answered
# literally, where the gate usefully drops generic profile noise — and pure
# personal-memory tokens (omscs/chris), keeping the gate ON for real factoid
# probes (the KO domain nouns enter via _augment_query_for_recall expansions).
_FACTOID_GATE_OFF_DOMAIN_TOKENS = frozenset(
    {
        "calendar",
        "calendars",
        "reminder",
        "reminders",
        "schedule",
        "music",
        "tts",
        "voice",
        "audio",
        "image",
        "images",
        "codex",
        "openclaw",
        "hermes",
        "tool",
        "tools",
        "tooling",
        "workflow",
        "workflows",
        "provider",
        "brain",
        "recall",
        "prefetch",
        "retrieval",
    }
)


# ── Stale OpenClaw per-agent workspace instruction docs ───────────────────
# `~/.openclaw/workspace-<agent>/AGENTS.md` and `TOOLS.md` are migration-era
# per-agent instruction/config files for a HISTORICAL runtime (Hermes is
# current). They dominate raw recall for durable-advice queries (cost/tooling/
# brain-quality) and inject stale workspace instructions. Drop them from
# retrieval for any query NOT actually about OpenClaw/the agents themselves;
# durable truth lives in canonical/distilled, not these workspace files.
_OPENCLAW_WORKSPACE_INSTRUCTION_RE = re.compile(
    r"\.openclaw/workspace-[^/]+/(?:agents|tools)\.md\b", re.IGNORECASE
)
_OPENCLAW_QUERY_TOKENS = frozenset(
    {
        "openclaw",
        "오픈클로",
        "jenna",
        "liz",
        "ellie",
        "sage",
        "market",
        "제나",
        "리즈",
        "엘리",
        "세이지",
        "마켓",
        "agent",
        "agents",
        "workspace",
        "에이전트",
    }
)


def _is_openclaw_workspace_instruction_result(result: dict) -> bool:
    meta = _result_metadata(result)
    path = str(result.get("path") or meta.get("source_path") or meta.get("path") or "")
    return bool(_OPENCLAW_WORKSPACE_INSTRUCTION_RE.search(path))


# ── Route-guarantee injection for /recall/v2 (mirror of active recall) ─────
# When a guaranteed route (codex_workflow, runtime_distinction, cost_billing, …)
# is matched but no retrieved row already carries the durable fact, inject the
# curated guarantee as a synthetic high-authority result so /recall/v2 — and the
# Hermes provider prefetch that calls it — surface the same current truth as
# /recall/active instead of stale/noisy rows. No exact-probe/task-id logic: the
# routes/facts come from brain_core/route_guarantees.yaml.


def _route_guarantee_served_by_results(guarantee, fused: list[dict]) -> bool:
    """True only when a DIRECT/CURRENT durable-truth row already states this
    guarantee fact (shares a strong majority of its DISTINCTIVE tokens), so the
    synthetic guarantee is not duplicated. A derived summary / distilled
    historical analysis does NOT serve a current route guarantee even if it
    overlaps wording — those rows lack the distinctive current/headless/bounded
    terms, and only direct current truth can substitute for the guarantee.
    Generic (authority tier + distinctive-token overlap), not per-intent."""
    gtokens = _guarantee_tokens(guarantee)
    if not gtokens:
        return False
    need = max(3, int(0.6 * len(gtokens)))
    for result in fused:
        if not isinstance(result, dict):
            continue
        if not _is_durable_truth_result(result):
            continue
        if len(gtokens & _tokenize_recall_text(_result_text(result))) >= need:
            return True
    return False


def _inject_route_guarantee_results(q: str, fused: list[dict]) -> None:
    """Append matched durable route-guarantee facts as synthetic high-authority
    results when retrieval under-serves them. Fail-open: any guarantee-load/match
    error leaves ``fused`` unchanged."""
    try:
        guarantees = _match_route_guarantees(q)
    except Exception:
        return
    if not guarantees:
        return
    existing_ids = {str(r.get("id")) for r in fused if isinstance(r, dict)}
    top_score = max(
        (float(r.get("score") or 0.0) for r in fused if isinstance(r, dict)),
        default=0.0,
    )
    rank_offset = 0.0
    for guarantee in guarantees:
        rid = f"route_guarantee:{guarantee.id}"
        if rid in existing_ids:
            continue
        if _route_guarantee_served_by_results(guarantee, fused):
            continue
        rank_offset += 5.0
        fused.append(
            {
                "id": rid,
                "title": f"{guarantee.route} route guarantee",
                "content": guarantee.text,
                "collection": "canonical",
                "source_type": "route_guarantee",
                "score": top_score + 20.0 + rank_offset,
                "governance": ["route_guarantee"],
                "metadata": {
                    "review_state": "accepted",
                    "category": "preference",
                    "authority_tier": "direct_current_truth",
                },
            }
        )


def _inject_personal_factoid_answer(q: str, fused: list[dict]) -> None:
    """Surface the durable answer for a PURE personal-fact probe whose fact lives
    in a raw_events hot-path atom (not in the vector store).

    Those atoms are reachable only by FTS literal-AND, and once retrieved the RRF
    pipeline can't reliably lift the decayed, low-authority row over broad
    session/canonical rows that merely mention the subject — so the clean fact
    gets buried. When no copy of a clean answer atom is already present, FTS the
    distinctive ASCII terms (acronyms/years/proper nouns survive cross-language)
    and inject the top durable atom that states the requested attributes as a
    high-authority answer. Mirror of _inject_route_guarantee_results.

    Precision is structural, never a probe string: the FTS AND-match requires
    every distinctive term, and the row must pass the whole-word factoid-overlap
    test and be neither a conversation transcript, a generic summary, an episodic
    coding/session log, nor a source/test-file quote — so a probe with no matching
    durable atom (teacher/childhood) injects nothing and stays empty. Fail-open.
    """
    if not _is_pure_personal_factoid_probe(q):
        return
    terms = _query_analyzer.personal_factoid_query_terms(q)
    ascii_focus = " ".join(dict.fromkeys(w for w in re.findall(r"[A-Za-z0-9]+", q) if w.lower() in terms))
    if not ascii_focus:
        return
    existing_ids = {str(r.get("id")) for r in fused if isinstance(r, dict)}
    try:
        from raw_events_fts import search as _raw_fts_search

        hits = _raw_fts_search(ascii_focus, limit=8) or []
    except Exception:
        return
    for hit in hits:
        text = str(hit.get("content") or "")
        rid = str(hit.get("id") or "")
        row = {
            "id": rid,
            "title": str(hit.get("title") or ""),
            "content": text,
            "collection": "raw_events_fts",
            "source_type": str(hit.get("raw_source_type") or hit.get("source_type") or ""),
            "score": 0.0,
        }
        if (
            _is_conversation_transcript_row(row)
            or _is_generic_summary_result(row)
            or _is_episodic_event_log_result(row, text)
            or _is_source_or_test_file_result(row)
            or _is_query_keyed_bridge_result(row)
        ):
            continue
        if _query_analyzer.personal_factoid_result_has_strong_attribute_overlap(q, text) is not True:
            continue
        # The clean answer atom is the FTS BM25 winner — if a copy is already in the
        # fused set, ranking governance already handles it; do not duplicate.
        if rid and (rid in existing_ids or f"raw_events:{rid}" in existing_ids):
            return
        top_score = max((float(r.get("score") or 0.0) for r in fused if isinstance(r, dict)), default=0.0)
        row["score"] = top_score + 20.0
        row["governance"] = ["personal_factoid_answer_injected"]
        fused.append(row)
        return


# First-failure flag so hook telemetry bugs surface once in logs instead of
# being silently swallowed on every request.
_hook_metrics_warned = False

router = APIRouter(dependencies=[Depends(verify_bearer)])


def _run_crag_retry(
    q: str,
    n: int,
    fused: list[dict],
    retry_fn,
) -> tuple[list[dict], int, dict, str | None]:
    """Phase M9 CRAG iterative retry: score first-hop confidence, optionally
    rewrite the query and run a single recursive retry, pick the
    higher-confidence result set.

    The retry is dispatched via the `retry_fn` callable (passed in by
    recall_v2 so this helper doesn't reach back into the module for the
    recursive recall_v2 reference). Capped at 1 retry to bound latency.

    Returns (fused_after, crag_ms, crag_telemetry, error_str_or_None).
    On any failure the input `fused` is returned unchanged with ms=0 and
    an empty telemetry; the caller writes timing["crag_error"] when
    `error_str_or_None` is not None.
    """
    try:
        from brain_core.crag import (
            expand_query as _crag_expand_query,
        )
        from brain_core.crag import (
            score_confidence as _crag_score,
        )
        from brain_core.crag import (
            should_iterate as _crag_should_iterate,
        )

        t_crag = time.time()
        # See _score_crag_first_hop docstring for the optional Self-RAG blend.
        confidence_report = _score_crag_first_hop(q, fused, n)
        telemetry: dict[str, Any] = {
            "first_hop_confidence": confidence_report.score,
            "first_hop_components": confidence_report.components,
            "iterated": False,
        }
        if _crag_should_iterate(confidence_report):
            rewritten = _crag_expand_query(q, fused[:3])
            if rewritten and rewritten != q:
                telemetry["expanded_query"] = rewritten
                # M7-WS7 C2 fix: retry_fn recurses with iterative=False AND
                # forces hyde=False, expand=False to prevent the inner call
                # from firing additional LLM dispatches. Worst case before
                # this fix: 1 outer HyDE + 3 outer expand + 1 CRAG rewrite
                # + 1 inner HyDE + 1 inner expand = up to 7 LLM calls per
                # req. After this fix: outer dispatches + 1 CRAG rewrite,
                # max.
                second_hop = retry_fn(rewritten)
                second_results = second_hop.results
                second_report = _crag_score(second_results[: max(n, 5)])
                telemetry["second_hop_confidence"] = second_report.score
                telemetry["iterated"] = True
                # Pick the higher-confidence result set
                if second_report.score > confidence_report.score:
                    fused = second_results
                    telemetry["selected"] = "second_hop"
                else:
                    telemetry["selected"] = "first_hop"
        crag_ms = int((time.time() - t_crag) * 1000)
        return fused, crag_ms, telemetry, None
    except Exception as _crag_err:
        log.warning("crag iterative path failed: %s", _crag_err)
        return fused, 0, {}, str(_crag_err)[:200]


def _decide_use_crag(q: str, iterative: bool) -> tuple[bool, str | None]:
    """M8.4: Adaptive-RAG router decides whether CRAG iterative recall fires.

    Default behavior honors the caller's `iterative=` flag. When the
    adaptive_rag.should_use_crag router is enabled, it can OVERRIDE the
    caller flag — disabling CRAG for SIMPLE queries (pure latency cost
    with no recall benefit) and enabling CRAG for MULTI queries even when
    the caller didn't ask.

    Returns (use_crag, reason). `reason` is the router's reason string when
    the router fired; None when the router was disabled or unavailable.
    On any failure, falls back to the caller's `iterative` flag.
    """
    use_crag = iterative
    reason: str | None = None
    try:
        from brain_core.adaptive_rag import should_use_crag as _ar_should_use

        use_crag, reason = _ar_should_use(q, caller_explicit=iterative)
    except Exception:
        use_crag = iterative
        reason = None
    return use_crag, reason


def _score_crag_first_hop(q: str, fused: list[dict], n: int):
    """First-hop CRAG confidence scoring + optional Self-RAG blend.

    Computes `confidence_report` from the top max(n,5) results, then —
    if BRAIN_SELF_RAG_ENABLED is on AND the critique returns a self_rag-
    sourced score — blends that into the report's score/components.

    Returns the (possibly mutated) confidence_report. Self-RAG failures
    are swallowed (best-effort layer; ~1s Jenna dispatch cost).

    Lifted from inline CRAG block so the heuristic + semantic-critique
    blend can be unit-tested independently of the recursive retry path.
    """
    from brain_core.crag import score_confidence as _crag_score

    confidence_report = _crag_score(fused[: max(n, 5)], query=q)
    # 2026-04-16 Tier 3 #11: Self-RAG (Asai 2023) semantic critique layer.
    # Off by default — costs ~1s Jenna call per iterative recall.
    try:
        from brain_core.self_rag import blend_with_heuristic as _blend_self_rag
        from brain_core.self_rag import critique as _self_rag_critique

        _sr = _self_rag_critique(q, fused[: max(n, 5)])
        if _sr.components.get("source") == "self_rag":
            blended = _blend_self_rag(_sr.score, confidence_report.score)
            confidence_report.score = blended
            confidence_report.components = {
                **confidence_report.components,
                "self_rag_score": _sr.score,
                "self_rag_components": _sr.components,
                "blended": True,
            }
    except Exception:
        pass
    return confidence_report


def _apply_parent_child_expand(fused: list[dict]) -> list[dict]:
    """M9.2: parent-child retrieval expand.

    When a child chunk wins the rank, swap its content for the wider parent
    chunk so the LLM consumer gets more context. Off by default; enabled via
    BRAIN_PARENT_CHILD_EXPAND in parent_child_expand.expand_to_parents.

    Runs BEFORE community injection so parents are available for both the
    child-expanded path and the community synthetic results. On import or
    expansion failure, the input is returned unchanged.
    """
    try:
        from brain_core.parent_child_expand import expand_to_parents as _pc_expand

        return _pc_expand(fused)
    except Exception as _pc_err:
        log.warning("parent-child expand failed: %s", _pc_err)
        return fused


def _inject_community_summaries(q: str, fused: list[dict]) -> tuple[list[dict], int]:
    """M8.7: inject GraphRAG community summaries for MULTI-class queries.

    When adaptive_rag classifies the query as MULTI (comparison, reasoning,
    multi-fact synthesis), the weekly-generated community summaries from
    the entity graph Louvain clusters are merged into fused with a synthetic
    rank. Gives the caller cross-document synthesis that single-doc retrieval
    can't provide.

    Cheap: summaries are pre-computed and sit in a small table with the
    entities indexed. get_summaries_matching does a single SELECT + a
    substring check against the query terms (<5ms).

    Off when BRAIN_COMMUNITY_SUMMARIES is unset, when the query is non-MULTI,
    or when no community matches the query entities.

    2026-04-16 R-2 scoring fix: score was previously hardcoded 95.0 which
    always placed community summaries at rank 1, overriding every Tier 1/2/3
    scoring fix above. Now scored relative to the current top result
    (0.85*top, clamped to [55,100]) so they can tiebreak or lead but not
    blindly dominate.

    Returns (new_fused, injected_count). On any failure, returns (fused, 0)
    and logs a warning.
    """
    try:
        from brain_core.adaptive_rag import classify as _ar_classify
        from brain_core.community_summaries import get_summaries_matching as _cs_match

        classification = _ar_classify(q)
        if classification.label != "multi":
            return fused, 0
        summaries = _cs_match(q, limit=2)
        if not summaries:
            return fused, 0
        top_score = float(fused[0].get("score", 0.0)) if fused else 0.0
        # Community injected at 0.85×top: meaningful but not always rank-1.
        synth_score = max(55.0, min(100.0, top_score * 0.85)) if top_score > 0 else 70.0
        synthetic: list[dict] = []
        for s in summaries:
            synthetic.append(
                {
                    "id": f"community:{','.join(s['entities'][:3])[:64]}",
                    "score": synth_score,
                    "source_type": "community",
                    "collection": "community_summaries",
                    "title": f"Community: {', '.join(s['entities'][:5])}",
                    "content": s["summary"],
                    "path": "graph/community/" + s.get("generated_at", ""),
                    "trust_tier": 2,  # derived, not canonical
                    "metadata": {
                        "entities": s["entities"],
                        "atom_count": s.get("atom_count", 0),
                        "generated_at": s.get("generated_at"),
                    },
                }
            )
        # Merge by score so they mix with real results rather than always
        # leading. MULTI queries still benefit because the score is high
        # enough to surface in top-3 typically.
        merged = sorted(fused + synthetic, key=lambda r: r.get("score", 0), reverse=True)
        return merged, len(synthetic)
    except Exception as _cs_err:
        log.warning("community summary inject failed: %s", _cs_err)
        return fused, 0


def _to_dashed_uuid(raw: str) -> str:
    """Hex32 UUID (dashes stripped) → canonical dashed form. Other shapes
    pass through unchanged. Used to normalize result ids before writing
    them to action_audit so downstream readers (recall_judge,
    contradiction propagation, audit dashboards) can round-trip them
    back to Qdrant points.
    """
    if not raw:
        return raw
    if len(raw) == 32 and "-" not in raw and all(c in "0123456789abcdef" for c in raw.lower()):
        return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"
    return raw


def _post_recall_side_effects(q: str, fused: list[dict], n: int, agent: str) -> None:
    """Run auto-feedback + action-audit writes for the recall response.

    Dispatched off the response path by _dispatch_post_recall_side_effects
    (either FastAPI BackgroundTasks or the search bg pool). M7-WS7 H3 fix:
    insert_action_audit was previously synchronous on the response path
    (0.5-30ms under SQLite writer contention). Both writes now share the
    same off-path dispatch so neither blocks the response.

    The action_audit write is wrapped in try/except so an atoms_store
    transient failure doesn't kill the feedback recorder side of the call.
    The retrieved_chroma_ids list is capped at 20 (audit storage budget).
    """
    _record_auto_feedback(q, fused[:n], agent)
    try:
        from brain_core.atoms_store import insert_action_audit as _iaa

        _iaa(
            route="/recall/v2",
            tool="brain_recall",
            actor=agent,
            query_text=q[:500],
            retrieved_chroma_ids=[
                _to_dashed_uuid(str(r.get("id") or r.get("chroma_id") or ""))[:64]
                for r in fused[:n]
                if r.get("id") or r.get("chroma_id")
            ][:20],
        )
    except Exception:
        pass


def _dispatch_post_recall_side_effects(
    q: str,
    fused: list[dict],
    n: int,
    agent: str,
    background: BackgroundTasks | None,
) -> None:
    """Submit _post_recall_side_effects off the response path.

    Preferred path: FastAPI BackgroundTasks (runs after response is sent).
    Fallback path: search bg pool (when no BackgroundTasks was injected,
    e.g. internal callers like brain_loop). If both fail, drop the writes —
    they're observability/feedback, not a hot path.
    """
    if background is not None:
        background.add_task(_post_recall_side_effects, q, fused, n, agent)
        return
    try:
        from brain_core.search_unified import _search_bg_pool

        _search_bg_pool.submit(_post_recall_side_effects, q, fused, n, agent)
    except Exception:
        pass


def _log_recall_gap(
    q: str,
    fused: list[dict],
    n: int,
    *,
    collection: str | None,
    domain: str | None,
    entity: str | None,
    source_type: str | None,
    since: str | None,
    until: str | None,
    as_of: str | None,
    include_history: bool,
    include_obsolete: bool,
) -> None:
    """Gap logging — record queries where the brain has nothing semantically
    close. Cross-encoder score is the only signal that reflects real semantic
    match; blended `score` is dominated by RRF ranks which always have a
    top-N winner even for gibberish queries.

    Heuristic: log when max CE score < 0.52 (model at the sigmoid midpoint,
    "I have no opinion"). Good queries see CE 0.55-0.75. Falls back to a
    blended-score threshold of 30.0 if CE was disabled.

    Only fires when the query is filter-free — filtered queries with no hits
    are usually intentional, not gaps.

    Appends one JSON line to BRAIN_DIR/logs/recall-gaps.jsonl. All
    exceptions swallowed (best-effort observability).
    Moved from /recall v1 on 2026-04-12; v1's max_score<5.0 threshold never fired.
    """
    try:
        filter_free = not (
            collection
            or domain
            or entity
            or source_type
            or since
            or until
            or as_of
            or include_history
            or include_obsolete
        )
        if not filter_free:
            return
        results_list = fused[:n]
        ce_scores = [
            float(r.get("cross_encoder_score", 0))
            for r in results_list
            if r.get("cross_encoder_score") is not None
        ]
        max_ce = max(ce_scores, default=0.0)
        # Fall back to blended score threshold if CE wasn't run (flag off)
        max_score = max((float(r.get("score", 0)) for r in results_list), default=0.0)
        is_gap = (
            len(results_list) == 0 or (ce_scores and max_ce < 0.52) or (not ce_scores and max_score < 30.0)
        )
        if not is_gap:
            return
        gap_log = BRAIN_DIR / "logs" / "recall-gaps.jsonl"
        gap_log.parent.mkdir(parents=True, exist_ok=True)
        with gap_log.open("a") as gf:
            gf.write(
                json.dumps(
                    {
                        "timestamp": datetime.now(UTC).isoformat(),
                        "query": q[:500],
                        "n_results": len(results_list),
                        "max_score": round(max_score, 2),
                        "max_ce_score": round(max_ce, 4) if ce_scores else None,
                        "endpoint": "/recall/v2",
                    }
                )
                + "\n"
            )
    except Exception:
        pass


def _log_retrieval_inhibition(fused: list[dict], q: str) -> None:
    """2026-04-16 Tier 3 #4 + R-10: retrieval-induced inhibition logging.

    Record top result as winner, ranks 2-5 as losers on this query cue.
    Dispatched to the search bg pool so we don't add SQLite write latency
    to the hot recall path (~15ms saved on p95).

    Only fires when at least 2 semantic_memory results land in the top-5;
    graph/canonical winners don't generate competition signals for the
    atom-level inhibition table.

    All exceptions swallowed — observability path, not a hot path. The
    bg-pool submit itself is fire-and-forget.
    """
    try:
        if fused and len(fused) >= 2:
            sm_results = [r for r in fused[:5] if r.get("collection") == "semantic_memory" and r.get("id")]
            if len(sm_results) >= 2:
                from retrieval_inhibition import log_competition as _log_comp

                from brain_core.search_unified import _search_bg_pool as _bg

                winner_id = sm_results[0]["id"]
                loser_ids = [r["id"] for r in sm_results[1:]]
                _bg.submit(_log_comp, winner_id, loser_ids, q)
    except Exception:
        pass


def _apply_metacognitive_surface_inplace(fused: list[dict], top_n: int) -> int:
    """Inject metacognitive signals (confidence, trust_score,
    pending_contradictions) into the top-N semantic_memory results.

    Two passes, both best-effort (each wrapped in try/except so a brain.db
    or Qdrant outage doesn't break the recall response):

      1. Confidence + trust_score from atoms.confidence (Bayesian-updated
         ledger), optionally Platt-calibrated via confidence_calibration.
         Surfaces `confidence` (calibrated), `confidence_raw` (uncalibrated),
         and `trust_score_current` on each row.

      2. Pending-contradictions count: query semantic_contradictions in
         Qdrant for unresolved rows referencing top-result IDs. Surfaces
         `pending_contradictions` (int count) on rows with open disputes.

    Mutates fused[:top_n] in place. Returns elapsed_ms for both passes
    combined (they share a single t_meta — the prior inline structure).
    Only semantic_memory results are touched; other collections are skipped
    because their atoms aren't in the brain.db confidence/trust ledger.
    """
    t_meta = time.time()

    # Pass 1: confidence + trust_score
    try:
        from atoms_store import _conn as _atoms_conn

        sm_ids = [
            r.get("id", "")
            for r in fused[:top_n]
            if isinstance(r, dict) and r.get("collection") == "semantic_memory" and r.get("id")
        ]
        if sm_ids:
            placeholders = ",".join("?" for _ in sm_ids)
            with _atoms_conn() as _c:
                rows = _c.execute(
                    f"SELECT chroma_id, confidence, trust_score "
                    f"FROM atoms WHERE chroma_id IN ({placeholders})",
                    sm_ids,
                ).fetchall()
            try:
                from confidence_calibration import apply_calibration as _apply_cal
            except Exception:

                def _apply_cal(x):
                    return x  # type: ignore

            conf_by_id = {
                r["chroma_id"]: {
                    "confidence_raw": round(float(r["confidence"] or 0.5), 3),
                    "confidence": round(float(_apply_cal(float(r["confidence"] or 0.5))), 3),
                    "trust_score": round(float(r["trust_score"] or 0.5), 3),
                }
                for r in rows
            }
            for r in fused[:top_n]:
                if r.get("collection") != "semantic_memory":
                    continue
                row = conf_by_id.get(r.get("id", ""))
                if row:
                    r["confidence"] = row["confidence"]
                    r["confidence_raw"] = row["confidence_raw"]
                    r["trust_score_current"] = row["trust_score"]
    except Exception:
        pass

    # Pass 2: pending-contradictions count
    try:
        if fused:
            top_ids = [r.get("id", "") for r in fused[:top_n] if r.get("id")]
            if top_ids:
                points = get_vector_store().get(
                    "semantic_contradictions",
                    filter={
                        "$or": [
                            {"memory_id_a": {"$in": top_ids}},
                            {"memory_id_b": {"$in": top_ids}},
                        ]
                    },
                    limit=100,
                    with_payload=True,
                    with_documents=False,
                )
                contra_count: dict[str, int] = {}
                for p in points:
                    meta = p.payload or {}
                    if meta.get("resolved"):
                        continue
                    a, b = meta.get("memory_id_a"), meta.get("memory_id_b")
                    if a:
                        contra_count[a] = contra_count.get(a, 0) + 1
                    if b:
                        contra_count[b] = contra_count.get(b, 0) + 1
                for r in fused[:top_n]:
                    rid = r.get("id", "")
                    if rid and rid in contra_count:
                        r["pending_contradictions"] = contra_count[rid]
    except Exception:
        pass

    return int((time.time() - t_meta) * 1000)


_CONTENT_ENRICHABLE_TYPES = frozenset(
    {
        "canonical-note",
        "distilled-note",
        "obsidian-note",
        "agent-config",
        "learning",
        "docker-compose",
        "nginx-conf",
    }
)
_CONTENT_ENRICH_MAX_FILE_BYTES = 4000
_CONTENT_ENRICH_ALLOWED_ROOTS = (
    BRAIN_DIR,
    KNOWLEDGE_DIR,
    OBSIDIAN_VAULT,
    OBSIDIAN_VAULT_ICLOUD,
    OBSIDIAN_VAULT_LOCAL,
    OPENCLAW_DIR,
)


def _apply_content_enrichment_inplace(fused: list[dict], top_n: int) -> int:
    """Read source files for the top-N file-backed results and replace
    the per-chunk `content` snippet with a richer excerpt (up to
    _CONTENT_ENRICH_MAX_FILE_BYTES) centered on the matched anchor.

    Retrieval ranking already happened; this just gives the caller (and
    downstream UIs / eval tools) richer context for the same document
    without disturbing rank order. The anchor lookup tries to find the
    first 120 chars of the chunk inside the live file — if found, a
    window around it is returned; if not (stale chunk, edited file), the
    file head is returned instead.

    Mutates `fused[i]['content']` in place. Returns elapsed_ms. Only
    types in `_CONTENT_ENRICHABLE_TYPES` are enriched; other result
    types and missing/unreadable files are skipped.
    """
    t_enrich = time.time()
    seen_paths: set[str] = set()
    for r in fused[:top_n]:
        path = r.get("path", "")
        if not path or path in seen_paths:
            continue
        rtype = r.get("type") or (r.get("metadata") or {}).get("type") or ""
        if rtype not in _CONTENT_ENRICHABLE_TYPES:
            continue
        try:
            p = _resolve_enrichable_path(path)
            if p is None:
                continue
            txt = p.read_text(errors="ignore")
        except Exception:
            continue
        chunk = r.get("content") or ""
        anchor = chunk[:120] if chunk else ""
        if anchor and anchor in txt:
            idx = txt.index(anchor)
            start = max(0, idx - 500)
            end = min(len(txt), idx + _CONTENT_ENRICH_MAX_FILE_BYTES - 500)
            r["content"] = txt[start:end]
        else:
            r["content"] = txt[:_CONTENT_ENRICH_MAX_FILE_BYTES]
        seen_paths.add(path)
    return int((time.time() - t_enrich) * 1000)


def _resolve_enrichable_path(path: str) -> Path | None:
    """Return a safe, resolved source path for content enrichment.

    Recall results can come from mutable vector-store metadata. Never trust a
    result's ``path`` field enough to read arbitrary local files; only enrich
    files that resolve under known Brain/knowledge/Obsidian/OpenClaw roots and
    reject symlinks escaping those roots.
    """

    try:
        resolved = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if not resolved.is_file():
        return None
    for root in _CONTENT_ENRICH_ALLOWED_ROOTS:
        try:
            root_resolved = Path(root).expanduser().resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            continue
        if resolved == root_resolved or resolved.is_relative_to(root_resolved):
            return resolved
    return None


def _apply_exclude_already_used(
    fused: list[dict],
    *,
    subject: str = "chris",
    relationship: str = "uses",
) -> tuple[list[dict], int, int]:
    """Phase G3 graph-constraint exclusion.

    Drop semantic_memory results whose extracted entities overlap the
    names returned by `(subject)-[:RELATES_TO {relationship}]->(t)` in
    Neo4j — i.e. atoms about tools/concepts the subject already uses.

    Why entity-link join (not raw text match): names like "react" (verb),
    "ghost" (idiom), "neo4j" (in unrelated graph-DB chatter) false-
    positive on word-boundary regex. The atom_entity table only links
    an atom to an entity when entity_graph.extract_and_store_entities
    (Sage LLM) judged it a real reference — that's the precision
    boundary we want. Other collections (canonical/obsidian/experience)
    don't run through the brain.db entity extractor, so we leave them
    unfiltered rather than fall back to a noisy regex.

    Returns (filtered_fused, dropped_count, elapsed_ms).
    """
    t_excl = time.time()
    excluded_count = 0
    try:
        from entity_graph import get_excluded_entities

        excluded_names = get_excluded_entities(subject, relationship)
    except Exception:
        excluded_names = set()

    if excluded_names:
        try:
            from atoms_store import _conn as _atoms_conn

            result_ids = [r.get("id") for r in fused if r.get("id")]
            excluded_lower = [n.lower() for n in excluded_names if n]
            if result_ids and excluded_lower:
                rid_ph = ",".join("?" for _ in result_ids)
                ex_ph = ",".join("?" for _ in excluded_lower)
                with _atoms_conn() as _c:
                    rows = _c.execute(
                        "SELECT DISTINCT atoms.chroma_id "
                        "FROM atoms "
                        "JOIN atom_entity ON atom_entity.atom_id = atoms.id "
                        "JOIN entities ON entities.id = atom_entity.entity_id "
                        f"WHERE LOWER(entities.name) IN ({ex_ph}) "
                        f"AND (atoms.chroma_id IN ({rid_ph}) OR "
                        f"     SUBSTR(atoms.chroma_id, INSTR(atoms.chroma_id, ':') + 1) IN ({rid_ph}))",
                        excluded_lower + result_ids + result_ids,
                    ).fetchall()
                drop_set: set[str] = set()
                for row in rows:
                    full = row["chroma_id"]
                    drop_set.add(full)
                    if ":" in full:
                        drop_set.add(full.split(":", 1)[1])
                if drop_set:
                    before = len(fused)
                    fused = [r for r in fused if r.get("id") not in drop_set]
                    excluded_count = before - len(fused)
        except Exception as exc:
            log.warning("exclude_already_used filter failed: %s", exc)

    return fused, excluded_count, int((time.time() - t_excl) * 1000)


def _run_token_rerank(q: str, fused: list[dict]) -> tuple[list[dict], int]:
    """Stage-1 token-overlap rerank.

    Idempotent (2026-04-16 fix): search_all already applied it per-variant
    and marked each result `_rerank_applied`. Calling `_rerank.rerank`
    again is a no-op score-wise; it only re-sorts. The score-promotion
    loop copies `rerank_score` into `score` so downstream sort + decay
    see the rerank result.

    Returns (fused, elapsed_ms). The caller writes
    `timing['rerank_ms'] = elapsed_ms`.
    """
    t_rerank = time.time()
    fused = _rerank.rerank(q, fused, top_k=None)
    for r in fused:
        r["score"] = r.get("rerank_score", r.get("score", 0))
    return fused, int((time.time() - t_rerank) * 1000)


def _run_cross_encoder_rerank(q: str, fused: list[dict]) -> tuple[list[dict], int | None, int | None]:
    """Stage-2 BGE cross-encoder rerank on the top window.

    Returns (fused, ce_top_k_or_none, elapsed_ms_or_none).

    The route writes the timing keys only when the values are not None:
      - both None if `BRAIN_CROSS_ENCODER_ENABLED` is false
      - both None if the cross-encoder import/call raises (stage-1 result
        is kept and a warning is logged)
      - both populated on success

    top_k=14 is the empirically-derived window (cut from 20 in 2026-04):
    extra slots rarely reshuffle the final top, and MPS batch time scales
    linearly with pair count — saves ~30ms p95 on single queries and a
    lot more under concurrent .predict() serialization.
    """
    ce_enabled = False
    try:
        from brain_core import config as _brain_config

        ce_enabled = bool(getattr(_brain_config, "BRAIN_CROSS_ENCODER_ENABLED", False))
    except Exception:
        ce_enabled = False

    if not ce_enabled:
        return fused, None, None

    t_ce = time.time()
    try:
        from brain_core.cross_encoder_rerank import (
            choose_cross_encoder_top_k,
            rerank_with_cross_encoder,
        )

        ce_top_k = choose_cross_encoder_top_k(q, fused, default_top_k=14)
        fused = rerank_with_cross_encoder(q, fused, top_k=ce_top_k)
        return fused, ce_top_k, int((time.time() - t_ce) * 1000)
    except Exception as _ce_err:
        log.warning("cross-encoder rerank failed, stage-1 result stands: %s", _ce_err)
        return fused, None, None


_KOREAN_INTENT_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "추천": ("recommendation", "preference", "decision"),
    "코덱스": (
        "codex",
        "codex through hermes",
        "interactive terminal",
        "tmux",
        "tui",
        "headless codex exec",
        "quality steering",
    ),
    "이미지": ("image", "image generation", "gpt images", "openai", "codex oauth", "subscription cli"),
    "음악": ("music", "audio generation", "no local generation"),
    "배경음악": ("background music", "music", "audio generation", "no local generation"),
    "음성": ("voice", "tts", "audio", "no local generation"),
    "tts": ("tts", "voice", "audio generation", "no local generation"),
    "모델": ("model", "local generation", "no local generation"),
    "설치": ("install", "local install", "local generation"),
    "새": ("new", "additional", "avoid new paid api"),
    "피해야": ("avoid", "no", "without"),
    "피해": ("avoid", "no", "without"),
    "캘린더": (
        "calendar",
        "apple calendar",
        "macos-calendar",
        "google-workspace-mcp",
        "primary tooling choices",
        "event",
    ),
    "달력": (
        "calendar",
        "apple calendar",
        "macos-calendar",
        "google-workspace-mcp",
        "primary tooling choices",
        "event",
    ),
    "리마인더": ("reminder", "apple-reminders", "apple reminders", "primary tooling choices", "task"),
    "일정": ("schedule", "calendar event", "reminder"),
    "수업": ("class", "class schedule", "calendar event", "school schedule"),
    "클래스": ("class", "class schedule", "calendar event", "school schedule"),
    "과금": ("billing", "cost", "paid api", "subscription"),
    "유료": ("paid", "paid api", "billing", "subscription"),
    "로컬": ("local", "local generation", "no local generation"),
    "클라우드": ("cloud", "hosting tradeoff", "cloud only when already available"),
    "진행상황": ("status", "progress", "live state", "kanban", "task"),
    "시작했어": ("started", "running", "live state", "kanban"),
    "완료": ("complete", "completed", "done", "live state", "kanban"),
    "작업": ("work", "task", "kanban", "live state"),
    "태스크": ("task", "kanban", "live state"),
    "칸반": ("kanban", "task", "live state"),
    # Loanword/runtime/comparison class terms — same KO→EN mechanism as
    # 코덱스→codex / 캘린더→calendar above (substring-matched, so particles like
    # 오픈클로랑 / 차이가 still hit). Lets language-agnostic intent gates fire for
    # KO without any probe-specific runtime detector.
    "오픈클로": ("openclaw",),
    "헤르메스": ("hermes",),
    "런타임": ("runtime",),
    "구분": ("distinction", "difference"),
    "차이": ("distinction", "difference"),
    "역사": ("historical", "history"),
    "과거": ("historical", "history", "past"),
}

_BUDGET_COST_TOKENS = {
    "api",
    "apis",
    "billing",
    "cost",
    "paid",
    "provider",
    "saas",
    "subscription",
    "과금",
    "유료",
}
_BUDGET_AVOID_TOKENS = {
    "additional",
    "another",
    "avoid",
    "extra",
    "free",
    "new",
    "no",
    "separate",
    "without",
    "무료",
}
_LOCAL_TOKENS = {"local", "locally", "ondevice", "로컬"}
_CLOUD_TOKENS = {"cloud", "hosted", "remote", "클라우드"}
_WORKFLOW_TOKENS = {
    "agent",
    "automation",
    "pipeline",
    "recommendation",
    "tool",
    "tools",
    "workflow",
    "workflows",
    "추천",
    "자동화",
}
_MEDIA_GENERATION_TOKENS = {"audio", "music", "tts", "voice", "background"}
_BUDGET_LOCAL_CLOUD_WORKFLOW_DOMAIN_TOKENS = _WORKFLOW_TOKENS - {"recommendation", "추천"}
_BUDGET_LOCAL_CLOUD_DOMAIN_STOP_TOKENS = (
    _BUDGET_COST_TOKENS
    | _BUDGET_AVOID_TOKENS
    | _LOCAL_TOKENS
    | _CLOUD_TOKENS
    | {
        "already",
        "and",
        "available",
        "choose",
        "chris",
        "constraint",
        "constraints",
        "existing",
        "first",
        "generation",
        "only",
        "or",
        "preference",
        "prefers",
        "recommendation",
        "run",
        "should",
        "unless",
        "use",
        "when",
        "which",
    }
)
_BUDGET_LOCAL_CLOUD_EXPANSIONS = (
    "avoid new paid api",
    "no separate paid api",
    "existing subscription",
    "local first",
    "cloud only when already available",
)
_AUTHORIZATION_TOKENS = {
    "allow",
    "allowed",
    "allowlist",
    "auth",
    "authorization",
    "authorized",
    "permission",
    "permitted",
    "허가",
    "허용",
    "권한",
    "권한준거라",
}
_TERMINAL_TELEGRAM_CONTEXT_TOKENS = {
    "fleet",
    "hermes",
    "ops",
    "telegram",
    "telegrams",
    "텔레그램",
    "toolset",
    "toolsets",
    "tools",
    "watchdog",
}
_TERMINAL_AUTHORIZATION_EXPANSION = (
    "market sage telegram terminal allowed authorized permission allowlist false positive"
)
# Concise evidence-shaped variants for terminal Telegram authorization recalls.
# Natural English prompts like "allowed by Chris?" over-weight generic Chris /
# Telegram allowlist memories; these variants preserve the exact operational
# correction vocabulary that retrieves the source authorization atom.
_TERMINAL_AUTHORIZATION_RESCUE_VARIANTS = (
    "fleet ops watchdog terminal false positive market sage",
    "이거 내가 권한준거라 false positive market sage telegram terminal",
)
_TERMINAL_AUTHORIZATION_EVIDENCE_MARKERS = (
    "false positive",
    "권한준거라",
    "hermes_ops_watchdog",
    "ops watchdog",
    "explicitly authorized",
    "authorized `terminal`",
    "telegram toolsets for hermes",
    "terminal access as allowed policy",
    "toolsets to use terminal",
    "telegram toolsets to use terminal",
    "allow market and sage",
)
_TERMINAL_AUTHORIZATION_LIVE_NOISE_MARKERS = (
    "erl_extraction",
    "memory hygiene",
    "chris profile preferences",
    "screen time patterns",
    "6-agent parallel source review",
    "parallel source review",
    "summarized from documented tech preferences",
    "consolidation week",
    "weekly arc",
    "clearer agent roles",
    "in-process brain scheduler",
    "external schedulers",
    "scheduler jobs",
)


def _augment_query_for_recall(q: str) -> str:
    """Append deterministic multilingual intent terms for provider-independent recall.

    This is intentionally local and cheap: no LLM, no paid API, no model load.
    Korean Telegram prompts often contain terse intent words (추천, 과금, 로컬)
    whose best matching durable memories are English canonical/preferences.
    """
    base = (q or "").strip()
    if not base:
        return ""
    lower = base.lower()
    additions: list[str] = []
    seen = set(_tokenize_recall_text(base))
    live_state_query = _is_live_state_query(base)
    live_state_expansion_markers = {"진행상황", "시작했어", "완료", "작업", "태스크", "칸반"}
    for marker, terms in _KOREAN_INTENT_EXPANSIONS.items():
        if marker not in lower and marker not in base:
            continue
        if marker in live_state_expansion_markers and not live_state_query:
            continue
        for term in terms:
            term_tokens = _tokenize_recall_text(term)
            if term_tokens and all(tok in seen for tok in term_tokens):
                continue
            additions.append(term)
            seen.update(term_tokens)
    if _is_budget_local_cloud_query(seen):
        for term in _BUDGET_LOCAL_CLOUD_EXPANSIONS:
            term_tokens = _tokenize_recall_text(term)
            if term_tokens and all(tok in seen for tok in term_tokens):
                continue
            additions.append(term)
            seen.update(term_tokens)
        if seen & _MEDIA_GENERATION_TOKENS:
            media_term = "cost conscious existing subscriptions integrations no local model hosting music TTS"
            term_tokens = _tokenize_recall_text(media_term)
            if term_tokens and not all(tok in seen for tok in term_tokens):
                additions.append(media_term)
                seen.update(term_tokens)
    if _is_terminal_telegram_authorization_query(seen):
        term = _TERMINAL_AUTHORIZATION_EXPANSION
        term_tokens = _tokenize_recall_text(term)
        if term_tokens and not all(tok in seen for tok in term_tokens):
            additions.append(term)
            seen.update(term_tokens)
    if not additions:
        return base
    return base + " " + " ".join(additions)


def _query_is_specific(query_tokens: set[str]) -> bool:
    broad = {"recommendation", "preference", "decision", "status", "progress", "task", "work"}
    return len(query_tokens - broad) >= 2


def _is_budget_local_cloud_query(query_tokens: set[str]) -> bool:
    has_budget = bool(query_tokens & _BUDGET_COST_TOKENS)
    avoids_new_cost = bool(query_tokens & _BUDGET_AVOID_TOKENS)
    compares_hosting = bool(query_tokens & _LOCAL_TOKENS) and bool(query_tokens & _CLOUD_TOKENS)
    has_domain_context = bool(query_tokens & (_WORKFLOW_TOKENS | _MEDIA_GENERATION_TOKENS))
    return has_budget and (avoids_new_cost or compares_hosting) and (compares_hosting or has_domain_context)


def _is_terminal_telegram_authorization_query(query_tokens: set[str]) -> bool:
    has_profiles = {"market", "sage"}.issubset(query_tokens)
    has_terminal = "terminal" in query_tokens
    has_authorization = bool(query_tokens & _AUTHORIZATION_TOKENS) or {"false", "positive"}.issubset(
        query_tokens
    )
    has_ops_context = bool(query_tokens & _TERMINAL_TELEGRAM_CONTEXT_TOKENS)
    return has_profiles and has_terminal and has_authorization and has_ops_context


def _is_terminal_telegram_authorization_evidence(text: str, result_tokens: set[str]) -> bool:
    lower = text.lower()
    if any(marker in lower for marker in _TERMINAL_AUTHORIZATION_EVIDENCE_MARKERS):
        return True
    return bool(
        {"fleet", "ops", "watchdog"} & result_tokens and {"false", "positive"}.issubset(result_tokens)
    )


def _is_terminal_telegram_authorization_result(result_tokens: set[str], text: str = "") -> bool:
    has_profiles = {"market", "sage"}.issubset(result_tokens)
    has_terminal = "terminal" in result_tokens
    has_telegram_context = bool(result_tokens & {"telegram", "텔레그램", "toolsets", "toolset"})
    has_authorization = bool(result_tokens & _AUTHORIZATION_TOKENS) or {"false", "positive"}.issubset(
        result_tokens
    )
    has_evidence = _is_terminal_telegram_authorization_evidence(text, result_tokens)
    return has_profiles and has_terminal and has_telegram_context and has_authorization and has_evidence


def _is_terminal_telegram_authorization_noise_result(
    result: dict, result_tokens: set[str], text: str
) -> bool:
    if _is_terminal_telegram_authorization_result(result_tokens, text):
        return False
    lower = text.lower()
    meta = _result_metadata(result)
    title = str(result.get("title") or meta.get("document_title") or meta.get("title") or "").lower()
    path = str(result.get("path") or meta.get("source_path") or meta.get("path") or "").lower()
    source_name = str(meta.get("source_name") or "").lower()
    marker_haystack = " ".join((title, path, source_name, lower))
    if "session keys" in title or "sessions_send" in lower:
        return True
    if "openclaw" in lower or "openclaw" in path:
        return True
    if "market is actively used" in lower or "used for brainstorming" in lower:
        return True
    if any(marker in marker_haystack for marker in _TERMINAL_AUTHORIZATION_LIVE_NOISE_MARKERS):
        return True
    has_live_topic = bool(result_tokens & {"chris", "market", "sage", "telegram", "텔레그램", "terminal"})
    has_authorization_words = bool(
        result_tokens & (_AUTHORIZATION_TOKENS | {"false", "positive", "governance"})
    )
    return has_live_topic and has_authorization_words


def _is_budget_local_cloud_constraint_result(result_tokens: set[str]) -> bool:
    has_budget = bool(result_tokens & _BUDGET_COST_TOKENS)
    has_constraint = bool(result_tokens & (_BUDGET_AVOID_TOKENS | {"existing"}))
    has_hosting_context = bool(result_tokens & (_LOCAL_TOKENS | _CLOUD_TOKENS))
    return has_budget and has_constraint and has_hosting_context


def _is_generic_api_troubleshooting_result(result_tokens: set[str]) -> bool:
    has_api_context = bool(result_tokens & {"api", "apis", "cloudflare"})
    has_troubleshooting = bool(
        result_tokens
        & {
            "auth",
            "bearer",
            "error",
            "external",
            "fix",
            "hex",
            "invalid",
            "key",
            "token",
            "troubleshooting",
        }
    )
    return has_api_context and has_troubleshooting


def _has_budget_local_cloud_domain_overlap(query_tokens: set[str], result_tokens: set[str]) -> bool:
    query_workflow = bool(query_tokens & _BUDGET_LOCAL_CLOUD_WORKFLOW_DOMAIN_TOKENS)
    result_workflow = bool(result_tokens & _BUDGET_LOCAL_CLOUD_WORKFLOW_DOMAIN_TOKENS)
    if query_workflow and result_workflow:
        return True

    query_domain_tokens = query_tokens - _BUDGET_LOCAL_CLOUD_DOMAIN_STOP_TOKENS
    result_domain_tokens = result_tokens - _BUDGET_LOCAL_CLOUD_DOMAIN_STOP_TOKENS
    return bool(query_domain_tokens & result_domain_tokens)


def _is_calendar_tooling_query(query_tokens: set[str]) -> bool:
    has_calendar = bool(query_tokens & {"calendar", "reminder", "reminders", "schedule", "event", "class"})
    has_reminders = bool(query_tokens & {"reminder", "reminders"})
    asks_tool = bool(
        query_tokens & {"tool", "tooling", "tools", "도구", "manage", "관리", "추천", "preference"}
    ) or any(tok.startswith("도구") or tok.startswith("관리") for tok in query_tokens)
    names_apple_calendar_reminders = "apple" in query_tokens and "calendar" in query_tokens and has_reminders
    # Naming BOTH calendar AND reminders (the two distinct PIM domains) is itself a
    # tooling-preference signal even without an explicit tool/도구 word or the
    # "apple" brand — "what should I remember about Chris using Calendar and
    # Reminders?" is asking which tools/flow he uses. Generic structural cue
    # (two co-named PIM nouns), not a probe string.
    names_calendar_and_reminders = "calendar" in query_tokens and has_reminders
    return has_calendar and (asks_tool or names_apple_calendar_reminders or names_calendar_and_reminders)


def _is_openclaw_calendar_skill_inventory_result(result: dict, text: str) -> bool:
    meta = _result_metadata(result)
    path = str(result.get("path") or meta.get("source_path") or meta.get("path") or "").lower()
    title = str(result.get("title") or meta.get("document_title") or "").lower()
    lower = text.lower()
    is_openclaw_inventory_path = "/.openclaw/workspace-" in path and path.endswith(
        ("/agents.md", "/tools.md")
    )
    if not is_openclaw_inventory_path:
        return False
    return (
        title in {"primary tooling choices", "google workspace", "tool failure recovery"}
        or "skill inventory" in lower
        or "primary tooling choices" in lower
        or "available tools" in lower
        or ("apple-reminders" in lower and ("macos-calendar" in lower or "google-workspace-mcp" in lower))
    )


def _is_calendar_tooling_preference_analysis_result(result: dict, text: str) -> bool:
    lower = text.lower()
    tokens = _tokenize_recall_text(text)
    has_apple_calendar = "apple calendar" in lower or {"apple", "calendar"}.issubset(tokens)
    has_apple_reminders = (
        "apple reminders" in lower or "apple-reminders" in lower or {"apple", "reminders"}.issubset(tokens)
    )
    has_preference_signal = bool(
        tokens
        & {
            "prefer",
            "prefers",
            "preferred",
            "preference",
            "primary",
            "tool",
            "tools",
            "흐름",
            "선호",
            "도구",
        }
    )
    return (
        _is_distilled_brain_analysis_result(result, text)
        and has_apple_calendar
        and has_apple_reminders
        and has_preference_signal
    )


def _is_primary_calendar_tooling_result(result: dict, text: str) -> bool:
    if _is_openclaw_calendar_skill_inventory_result(result, text):
        return False
    lower = text.lower()
    return (
        _is_calendar_tooling_preference_analysis_result(result, text)
        or "primary tooling choices" in lower
        or ("apple-reminders" in lower and ("macos-calendar" in lower or "google-workspace-mcp" in lower))
    )


def _is_personal_calendar_instance(result: dict, text: str) -> bool:
    collection = str(result.get("collection") or "").lower()
    lower = text.lower()
    return collection == "personal" and (
        "reminders://" in lower or "reminder:" in lower or "calendar event" in lower
    )


def _is_live_state_snapshot_result(result: dict, text: str) -> bool:
    meta = _result_metadata(result)
    lower = text.lower()
    title = str(result.get("title") or meta.get("document_title") or "").lower()
    path = str(result.get("path") or meta.get("source_path") or meta.get("path") or "").lower()
    doc_type = str(meta.get("document_type") or meta.get("type") or "").lower()
    return (
        "live state snapshot" in lower
        or "active goals and focus" in lower
        or "current-state snapshots" in lower
        or "live_state_snapshot cron" in lower
        or ("manual focus items" in title and ("active goals" in lower or "active_goals" in path))
        or "live_state_snapshot" in path
        or "/live_state/" in path
        or (doc_type == "canonical-note" and path.endswith("/live_state/active_goals.md"))
    )


def _is_brain_failure_note_result(text: str) -> bool:
    lower = text.lower()
    return (
        "failed to surface" in lower
        or "brain_recall" in lower
        or ("what happened" in lower and "brain" in lower)
        or "brain failure" in lower
    )


def _is_calendar_tooling_offtopic_result(result: dict, text: str) -> bool:
    if _is_openclaw_calendar_skill_inventory_result(result, text):
        return True
    lower = text.lower()
    exact_tooling_markers = (
        "primary tooling choices",
        "apple-reminders",
        "macos-calendar",
        "google-workspace-mcp",
        "todoist",
        "things-mac",
    )
    return not any(marker in lower for marker in exact_tooling_markers)


def _is_broad_tool_recommendation_query(query_tokens: set[str]) -> bool:
    has_recommendation = bool(
        query_tokens
        & {"recommend", "recommendation", "tool", "tools", "useful", "max", "help", "no", "noise"}
    )
    has_chris_context = "chris" in query_tokens or bool(query_tokens & {"preferences", "preference", "선호"})
    domain_tokens = query_tokens - {
        "recommend",
        "recommendation",
        "tool",
        "tools",
        "useful",
        "chris",
        "given",
        "his",
        "preferences",
        "preference",
        "no",
        "noise",
        "noisy",
        "max",
        "help",
    }
    return has_recommendation and has_chris_context and len(domain_tokens) <= 2


# ── Generic source-quality contract (provenance-only, topic-agnostic) ─────
# The recall corpus mixes direct durable truth (semantic memories, accepted
# canonical facts/preferences/decisions) with derived/secondary formats
# (summaries, reflections, session/weekly digests, procedure/voyager logs,
# distilled brain-analysis meta). For any query that is not explicitly asking
# for a summary, the former should outrank the latter. These two classifiers
# express that contract from PROVENANCE signals (collection / category /
# review_state / doc format) — never topic markers — so one rule serves every
# recall class instead of a per-probe boost/penalty pair.


# Episodic event/coding-session capture shapes. Agent-session logs and raw
# coding-events record *what happened in a session*, not durable truth, so they
# are low-authority for any non-summary recall — the same provenance contract as
# summaries/reflections. Lesson/root-cause scaffolds ("## Why this matters") are
# deliberately NOT listed: those carry durable learnings worth surfacing.


def _has_direct_tool_candidate_marker(result: dict, text: str) -> bool:
    """True when a broad recommendation result names an actionable candidate.

    Broad tool-recommendation probes need candidate rows (e.g. `claude_code`,
    `brain-reflect:nightly`) or concise preference evidence. Generic OpenClaw
    summaries mention tools/preferences often enough to match semantically, but
    they are not themselves recommendations.
    """
    title = str(result.get("title") or _result_metadata(result).get("document_title") or "").strip().lower()
    if re.fullmatch(r"[a-z][a-z0-9]*(?:[_:-][a-z0-9]+)+", title):
        return True
    candidate_markers = (
        "tool:",
        "candidate:",
        "use `",
        "run `",
    )
    lower = text.lower()
    return any(marker in lower for marker in candidate_markers)


def _is_tool_recommendation_preference_evidence(text: str) -> bool:
    tokens = _tokenize_recall_text(text)
    has_recommendation = bool(tokens & {"recommend", "recommendation", "recommendations"})
    has_signal = bool(tokens & {"noise", "noisy", "leverage", "useful", "help", "helpful", "evidence"})
    return (
        has_recommendation and has_signal and bool(tokens & {"chris", "preference", "preferences", "prefers"})
    )


def _is_broad_tool_recommendation_noise_result(result: dict, text: str) -> bool:
    if _has_direct_tool_candidate_marker(result, text) or _is_tool_recommendation_preference_evidence(text):
        return False
    lower = text.lower()
    title = str(result.get("title") or _result_metadata(result).get("document_title") or "").lower()
    return (
        _is_generic_summary_result(result)
        or "openclaw" in lower
        or "openclaw" in title
        or lower.lstrip().startswith("### details")
    )


def _is_openclaw_hermes_distinction_query(query_tokens: set[str]) -> bool:
    return {"openclaw", "hermes"}.issubset(query_tokens) and bool(
        query_tokens & {"current", "runtime", "historical", "distinction", "history"}
    )


def _is_openclaw_hermes_distinction_result(result_tokens: set[str], text: str) -> bool:
    lower = text.lower()
    return {"openclaw", "hermes"}.issubset(result_tokens) and (
        bool(result_tokens & {"current", "runtime", "historical", "distinction"})
        or "hermes agent is" in lower
        or "current runtime is hermes" in lower
    )


def _is_openclaw_setup_noise_result(result: dict, text: str) -> bool:
    lower = text.lower()
    meta = _result_metadata(result)
    title = str(result.get("title") or meta.get("document_title") or "").lower()
    path = str(result.get("path") or meta.get("source_path") or meta.get("path") or "").lower()
    return (
        "openclaw multi-agent setup documentation" in title
        or "/.openclaw/workspace-" in path
        or "openclaw-setup" in path
        or "sub-agent configuration" in lower
        or ("active hours for heartbeat" in lower and "openclaw" in lower)
        or ("openclaw setup" in lower and "current runtime is hermes" not in lower)
    )


def _is_openclaw_hermes_handoff_noise_result(result: dict, text: str) -> bool:
    """True for task/test handoff rows about recall quality, not durable truth.

    These rows often quote the exact OpenClaw/Hermes acceptance probe plus
    nearby ``live_state``/setup text. They are useful run history, but they are
    meta-evidence about this tuning task rather than the current-runtime fact
    itself, so they should not receive the same distinction boost.
    """
    lower = text.lower()
    meta = _result_metadata(result)
    source_hint = " ".join(
        str(part or "")
        for part in (
            result.get("title"),
            result.get("path"),
            meta.get("source_name"),
            meta.get("source_path"),
        )
    ).lower()
    marker_haystack = f"{source_hint}\n{lower[:1500]}"
    return (
        "work kanban task t_" in marker_haystack
        or "acceptance probe" in marker_haystack
        or "focused tests passed" in marker_haystack
        or "review-required handoff" in marker_haystack
        or "dirty patch" in marker_haystack
        or "verdict: partial" in marker_haystack
        or "generic regression" in marker_haystack
        or "generic_recipe_knowledge_gap" in marker_haystack
        or "spot check" in marker_haystack
        or "no setup/live_state" in marker_haystack
    )


def _is_openclaw_hermes_distinction_noise_result(result: dict, text: str) -> bool:
    """True for accepted non-answer rows in OpenClaw/Hermes distinction recall."""
    return (
        _is_openclaw_setup_noise_result(result, text)
        or _is_openclaw_hermes_handoff_noise_result(result, text)
        or _is_live_state_snapshot_result(result, text)
    )


def _is_codex_skill_sync_noise_result(result: dict, text: str) -> bool:
    lower = text.lower()
    title = str(result.get("title") or _result_metadata(result).get("document_title") or "").lower()
    path = str(result.get("path") or _result_metadata(result).get("source_path") or "").lower()
    haystack = f"{title}\n{path}\n{lower[:1000]}"
    if _is_codex_hermes_tui_result(_tokenize_recall_text(haystack), haystack) and any(
        marker in haystack for marker in ("prefers", "preference", "선호")
    ):
        return False
    return (
        "codex/claude code skill" in haystack
        or "skills/autonomous-ai-agents" in haystack
        or "skill sync" in haystack
        or ("codex" in haystack and "claude code" in haystack and "skill" in haystack)
    )


def _is_codex_hermes_tui_query(query_tokens: set[str]) -> bool:
    return "codex" in query_tokens and bool(
        query_tokens
        & {
            "hermes",
            "tmux",
            "tui",
            "headless",
            "steering",
            "quality",
            "coding",
            "preference",
            "recommendation",
            "어떻게",
            "좋아",
        }
    )


def _is_codex_hermes_tui_result(result_tokens: set[str], text: str) -> bool:
    lower = text.lower()
    return {"codex", "hermes"}.issubset(result_tokens) and (
        bool(result_tokens & {"tmux", "tui", "headless", "interactive"})
        or "terminal-like" in lower
        or "terminal like" in lower
    )


def _is_old_claude_code_restriction_noise(result_tokens: set[str], text: str) -> bool:
    lower = text.lower()
    return (
        "claude" in result_tokens
        and "code" in result_tokens
        and "codex" not in result_tokens
        and ("restriction" in result_tokens or "plan-mode" in lower or "usage caveat" in lower)
    )


def _is_brain_contract_result(result: dict, text: str) -> bool:
    meta = _result_metadata(result)
    lower = text.lower()
    title = str(result.get("title") or meta.get("document_title") or "").lower()
    path = str(result.get("path") or meta.get("source_path") or "").lower()
    return (
        "brain contract" in title
        or "memory protocols" in title
        or "brain contract" in lower
        or "mcp.servers.brain" in lower
        or path.endswith("/brain_contract.md")
    )


_BRAIN_QUALITY_SUBSYSTEM_TOKENS = {
    "brain",
    "recall",
    "prefetch",
    "retrieval",
    "브레인",
    "리콜",
    "검색품질",
}
_BRAIN_QUALITY_BROAD_TOKENS = {
    "context",
    "noise",
    "noisy",
    "eval",
    "evaluation",
    "score",
    "quality",
    "fine",
    "tuning",
    "노이즈",
    "평가",
    "품질",
    "튜닝",
}
_BRAIN_QUALITY_GENERIC_MARKERS = (
    "knowledge gap bridge: brain system dependency",
    "brain depends on fastapi brain-server",
    "turning brain and openclaw from clever infrastructure",
    "native qdrant",
    "native ollama",
    "underused tools",
    "brain_decide",
    "search index",
    "qdrant vector store",
    "fastapi server",
    "port 8791",
)


def _is_brain_quality_query(q: str) -> bool:
    text = _augment_query_for_recall(q)
    if "brain_decide" in (text or "").lower():
        return True
    tokens = _tokenize_recall_text(text)
    return bool(tokens & _BRAIN_QUALITY_SUBSYSTEM_TOKENS) and bool(tokens & _BRAIN_QUALITY_BROAD_TOKENS)


def _is_stale_generic_quality_result(result: dict, q: str) -> bool:
    if not _is_brain_quality_query(q):
        return False
    if _is_positive_summary_intent_query(q):
        return False
    query_text = (q or "").lower()
    haystack = _result_text(result).lower()
    for marker in _BRAIN_QUALITY_GENERIC_MARKERS:
        if marker in haystack and marker not in query_text:
            return True
    # Weekly/session summary blobs are usually stale noise for concrete Brain
    # quality fixes unless the user explicitly asks for a recap.
    return _is_generic_summary_result(result) and not _is_summary_excluded_query(q)


# ── Generic out-of-domain (world-knowledge) gate ─────────────────────────
# The personal-memory corpus is about Chris's world (his preferences, tools,
# systems, work). A prompt with NO anchor into that world whose only content is
# an external topic (after stripping generic ask/procedure scaffolding) is a
# world-knowledge request the corpus cannot answer — recipes, general how-tos,
# trivia. This generalizes the English-only recipe gate to any such prompt in
# any language; the recipe case is just one instance.
# Domain anchors: personal-memory tokens plus first-person/tooling/work
# vocabulary. A query touching any of these is about Chris's world, not generic
# world-knowledge. Class-level vocabulary, not probe strings.


def _is_pure_personal_factoid_probe(q: str) -> bool:
    """A PURE personal-fact probe: names a personal subject (Chris/my/user) with
    distinctive attribute terms, carries NO tool/media/runtime domain noun, no
    matched route guarantee, and is not an explicit summary request.

    For this class the strict factoid contract applies (literal-answer boost,
    transcript demotion, whole-word overlap gate); tooling/cost/route prompts are
    excluded — their answers use synonym-rich vocabulary. Mirrors the Hermes
    provider's apply_factoid_gate scoping so both surfaces share ONE factoid-probe
    contract. Shared analyzer class (EN+KO), never a probe string.
    """
    if not _query_analyzer.personal_factoid_query_terms(q):
        return False
    if _is_positive_summary_intent_query(q):
        return False
    if _tokenize_recall_text(_augment_query_for_recall(q)) & _FACTOID_GATE_OFF_DOMAIN_TOKENS:
        return False
    return not _match_route_guarantees(q)


def _apply_retrieval_quality_filter(q: str, fused: list[dict]) -> list[dict]:
    """Post-rank quality pass shared by raw recall and other Brain tool paths.

    It removes stale Brain-quality summary noise and collapses exact/near
    duplicate memories while keeping the best canonical/truth-scored row.
    """
    if not fused:
        return fused
    summary_excluded = _is_summary_excluded_query(q)
    generic_recipe_query = _is_generic_recipe_query(q)
    # Genuine out-of-domain world-knowledge prompt: an anchorless recipe / generic
    # how-to with NO matched durable route. A named-runtime/tool/cost topic the OOD
    # classifier flags only because it pairs two anchors (OpenClaw + Hermes) carries
    # a matched route guarantee and is exempted, keeping its distinction rows.
    out_of_domain_query = _is_out_of_domain_world_knowledge_query(q) and not _match_route_guarantees(q)
    # Personal-attribute identity/attribute guard: a self/possessive attribute
    # query ("what is my address?", "when is Chris's birthday?", "내 주소가 뭐야?",
    # "what is Ellie's phone number?") targets ONE identity's ONE attribute. Keep
    # ONLY a row that states the SAME subject's SAME attribute; a different
    # identity's value, a different attribute of the same identity, or an unrelated
    # row is identity/attribute contamination. A legitimate explicit third-person
    # query keeps its matching row; birthday is one instance of this class. Shared
    # analyzer class (EN+KO), never a hardcoded name list or probe string.
    personal_attribute_query = _query_analyzer.personal_attribute_query_binding(q) is not None
    # Scope the strict whole-word factoid overlap drop to PURE personal-fact
    # probes (see _is_pure_personal_factoid_probe): a tooling/cost/route prompt is
    # answered with DIFFERENT vocabulary than its literal terms ("Apple Calendar"
    # for "Calendar and Reminders"), and Korean prompts glue particles onto the
    # nouns (리마인더는/일정이랑) that no atom carries — so the literal-overlap gate
    # would wrongly EMPTY those. Genuine factoid probes (teacher/OMSCS …) keep it.
    pure_personal_factoid_probe = _is_pure_personal_factoid_probe(q)
    openclaw_hermes_distinction_query = _is_openclaw_hermes_distinction_query(
        _tokenize_recall_text(_augment_query_for_recall(q))
    )
    # Only keep stale per-agent OpenClaw workspace instruction docs (AGENTS.md/
    # TOOLS.md) when the query is actually about OpenClaw/the agents; otherwise
    # they inject migration-era instructions over current durable truth.
    openclaw_targeted_query = bool(_tokenize_recall_text(q) & _OPENCLAW_QUERY_TOKENS)
    candidates = []
    for result in fused:
        if not isinstance(result, dict):
            continue
        result_text = _result_text(result)
        if _is_stale_generic_quality_result(result, q):
            continue
        if (
            personal_attribute_query
            and _query_analyzer.personal_attribute_result_matches_query(q, result_text) is False
        ):
            continue
        if pure_personal_factoid_probe:
            factoid_overlap = _query_analyzer.personal_factoid_result_has_strong_attribute_overlap(
                q, result_text
            )
            if factoid_overlap is False:
                continue
            # Disjoint-script rows answer None (a pure-Hangul probe can never
            # whole-word-overlap an English-only row, so empty overlap is a
            # script artifact). Where overlap is unjudgeable, fall back to
            # source authority: durable truth surfaces stay, derived noise
            # (reflections/sessions/summaries) drops exactly as the judgeable
            # path would have dropped it.
            if factoid_overlap is None and _source_authority.is_low_authority_result(result, result_text):
                continue
        if not openclaw_targeted_query and _is_openclaw_workspace_instruction_result(result):
            continue
        if summary_excluded and _is_generic_summary_result(result):
            continue
        if generic_recipe_query and not _is_recipe_result(result):
            continue
        # Out-of-domain world-knowledge prompt: the personal corpus is not the
        # right source even when it happens to hold an EXACT recipe/procedure/graph
        # memory (a stored "tomato_pasta_sauce_recipe" procedure, a "tomato pasta
        # recipe" graph concept) — the model answers world-knowledge from its own
        # knowledge, and surfacing those rows is off-domain leakage. Drop EVERY row
        # so the set is empty. In-domain prompts are never classified OOD here (they
        # carry a world-knowledge anchor or a matched route), so this only empties
        # genuine world-knowledge asks.
        if out_of_domain_query:
            continue
        if openclaw_hermes_distinction_query and _is_openclaw_hermes_distinction_noise_result(
            result, result_text
        ):
            continue
        candidates.append(result)
    best_by_key: dict[str, dict] = {}
    for result in candidates:
        key = _near_duplicate_key(result) or str(result.get("id") or result.get("path") or "")
        if not key:
            key = str(id(result))
        prev = best_by_key.get(key)
        if prev is None or _quality_rank_tuple(result) > _quality_rank_tuple(prev):
            best_by_key[key] = result
    deduped: list[dict] = []
    kept_signatures: list[str] = []
    for result in sorted(best_by_key.values(), key=lambda r: float(r.get("score") or 0.0), reverse=True):
        sig = _near_duplicate_key(result)
        if _is_near_duplicate_signature(sig, kept_signatures):
            continue
        deduped.append(result)
        if sig:
            kept_signatures.append(sig)
    return deduped


def _apply_recall_governance_inplace(q: str, fused: list[dict]) -> None:
    """Server-side ranking governance for /recall/v2.

    Boost accepted canonical truth and durable preference/decision/correction
    rows; penalize broad weekly/summary/raptor documents for specific queries
    when a non-summary topical candidate exists. Mutates scores in place and
    annotates each touched row with a `governance` reason list for eval/debug.
    """
    query_tokens = _tokenize_recall_text(_augment_query_for_recall(q))
    if not query_tokens or not fused:
        return

    specific = _query_is_specific(query_tokens)
    budget_local_cloud_query = _is_budget_local_cloud_query(query_tokens)
    non_summary_topical_exists = any(
        not _is_generic_summary_result(r) and len(query_tokens & _tokenize_recall_text(_result_text(r))) >= 2
        for r in fused
        if isinstance(r, dict)
    )
    calendar_tooling_query = _is_calendar_tooling_query(query_tokens)
    broad_tool_recommendation_query = _is_broad_tool_recommendation_query(query_tokens)
    terminal_telegram_authorization_query = _is_terminal_telegram_authorization_query(query_tokens)
    openclaw_hermes_distinction_query = _is_openclaw_hermes_distinction_query(query_tokens)
    codex_hermes_tui_query = _is_codex_hermes_tui_query(query_tokens)
    summary_excluded = _is_summary_excluded_query(q)
    positive_summary_intent = _is_positive_summary_intent_query(q)
    personal_attribute_binding = _query_analyzer.personal_attribute_query_binding(q)
    pure_personal_factoid_probe = _is_pure_personal_factoid_probe(q)

    for result in fused:
        if not isinstance(result, dict):
            continue
        reasons: list[str] = []
        delta = 0.0
        meta = _result_metadata(result)
        category = _result_category(result)
        collection = str(result.get("collection") or "").lower()
        review_state = str(meta.get("review_state") or result.get("review_state") or "").lower()
        title_path_tokens = _tokenize_recall_text(
            " ".join(str(result.get(k) or "") for k in ("title", "path"))
        )
        all_tokens = _tokenize_recall_text(_result_text(result))
        title_path_overlap = len(query_tokens & title_path_tokens)
        total_overlap = len(query_tokens & all_tokens)
        result_text = _result_text(result)

        if collection == "canonical" and review_state in {"accepted", "approved", "canonical"}:
            delta += 18.0
            reasons.append("canonical_accepted")
        if category in _TRUTH_CATEGORIES:
            delta += 18.0
            reasons.append("specific_truth")
        if collection == "canonical" and category in _TRUTH_CATEGORIES:
            delta += 8.0
            reasons.append("canonical_truth")
        if title_path_overlap:
            delta += min(24.0, 6.0 * title_path_overlap)
            reasons.append("title_path_relevance")
        if total_overlap >= 3:
            delta += 8.0
            reasons.append("topical_density")

        if (
            budget_local_cloud_query
            and not _is_generic_summary_result(result)
            and _is_budget_local_cloud_constraint_result(all_tokens)
            and _has_budget_local_cloud_domain_overlap(query_tokens, all_tokens)
        ):
            delta += 80.0
            reasons.append("budget_local_cloud_constraint")
            if category in _TRUTH_CATEGORIES:
                # A specifically paid-API/local-hosting prompt is answered by the
                # STATED durable cost preference/decision (existing subscriptions,
                # no new paid API, no local hosting), not by a generic procedural
                # heuristic that merely shares the "recommending a new tool"
                # framing. Lift the stated cost-constraint TRUTH row decisively so
                # an IF-THEN "how to recommend a tool" extraction — which earns the
                # generic durable-truth prior on its strong lexical match — cannot
                # preempt it. Class-level (truth category + budget-constraint
                # result + budget query), never a probe string.
                delta += 45.0
                reasons.append("budget_constraint_truth_priority")

        if (
            budget_local_cloud_query
            and _is_generic_api_troubleshooting_result(all_tokens)
            and not _has_budget_local_cloud_domain_overlap(query_tokens, all_tokens)
        ):
            delta -= 55.0
            reasons.append("generic_api_troubleshooting_penalty")

        if (
            budget_local_cloud_query
            and not _has_budget_local_cloud_domain_overlap(query_tokens, all_tokens)
            and not _is_budget_local_cloud_constraint_result(all_tokens)
            and (total_overlap >= 2 or collection in {"experience", "semantic_memory"})
        ):
            delta -= 80.0
            reasons.append("budget_offtopic_penalty")

        if _is_brain_failure_note_result(result_text) and not _is_brain_quality_query(q):
            delta -= 140.0
            reasons.append("brain_failure_note_penalty")

        live_state_snapshot = _is_live_state_snapshot_result(result, result_text)
        if live_state_snapshot:
            delta -= 180.0
            reasons.append("live_state_snapshot_penalty")

        low_authority = _is_low_authority_result(result, result_text)

        # Generic source-quality prior (provenance-based, topic-agnostic): direct
        # durable truth outranks derived summary/reflection/session/procedure/
        # voyager/meta rows for any query not explicitly asking for a summary.
        # One contract serves every recall class; the per-intent branches below
        # remain as sharper, topic-specific reinforcements.
        if not positive_summary_intent:
            if _is_durable_truth_result(result):
                delta += 30.0
                reasons.append("durable_truth_priority")
            elif low_authority:
                delta -= 45.0
                reasons.append("low_authority_source_penalty")

        # Vanished-source provenance: the row's absolute local source file no
        # longer exists (deleted/moved/retired doc, e.g. a removed agent
        # workspace). Living documents outrank it for any current query —
        # demote decisively, never drop (the content may still be the only
        # historical record). Purely provenance-derived, no path/topic markers.
        if _is_vanished_source_result(result):
            delta -= 160.0
            reasons.append("vanished_source_penalty")

        # Query-keyed bridge atom: content hard-bound to one literal query
        # phrasing ("For the exact query X: ..."). A data-level retrieval
        # hack — it wins by echoing the keyed query, masking real gaps and
        # polluting paraphrases. Demote decisively, never drop (the embedded
        # fact may still be the only copy); it must also never ride the
        # targeted answer boosts below past the source-anchored row it
        # plagiarizes. Format-derived, no topic/probe/value markers.
        query_keyed_bridge = _is_query_keyed_bridge_result(result)
        if query_keyed_bridge:
            delta -= 160.0
            reasons.append("query_keyed_bridge_penalty")

        if (
            personal_attribute_binding is not None
            and not query_keyed_bridge
            and _query_analyzer.personal_attribute_result_matches_query(q, result_text) is True
            and not _is_generic_summary_result(result)
            and not _is_source_or_test_file_result(result)
        ):
            # Scoped owner+attribute queries ("what is my address", "Chris birthday",
            # "내 주소가 뭐야") need the direct matching row to reach the served
            # window before the identity/attribute guard runs. Otherwise a short,
            # exact personal fact can be crowded out by high-scoring unrelated rows
            # and the guard sees only noise, producing a false zero-result response.
            # This is generic: same shared analyzer binding as the guard, excluding
            # summary/source-file quotation noise, with no probe/value/name-specific
            # logic. The boost intentionally applies to direct personal/raw-event
            # rows as well as canonical semantic truth because personal-collection
            # facts can be typed as notes/messages rather than truth categories.
            delta += 240.0
            reasons.append("personal_attribute_match_priority")

        if pure_personal_factoid_probe:
            # Pure personal-fact probe ("What should I remember about Chris OMSCS
            # Fall 2026?"): the durable answer atom often lives in a low-authority
            # raw_events hot-path row that earns no canonical/truth boost, so a
            # generic canonical summary / graph-entity stub / session log that
            # merely mentions the subject crowds it out. Boost a row that states
            # the requested distinctive attribute terms as WHOLE words (the literal
            # answer), and demote a conversation/session transcript that only QUOTES
            # them — mirror of the provider's answer-only personal-fact contract.
            # Shared analyzer overlap (EN+KO), provenance-neutral, no probe string.
            if _is_conversation_transcript_row(result):
                delta -= 200.0
                reasons.append("personal_factoid_transcript_penalty")
            elif (
                _query_analyzer.personal_factoid_result_has_strong_attribute_overlap(q, result_text) is True
                and not query_keyed_bridge
                and not _is_generic_summary_result(result)
                and not _is_source_or_test_file_result(result)
                # A graph-entity node ("Entity: omscs fall 2026 (concept)") restates
                # the query terms as a concept stub, not a durable answer — exclude
                # it so the answer-bearing memory atom, not the stub, leads.
                and collection != "graph"
            ):
                delta += 120.0
                reasons.append("personal_factoid_answer_priority")

        if openclaw_hermes_distinction_query:
            openclaw_setup_noise = _is_openclaw_setup_noise_result(result, result_text)
            openclaw_handoff_noise = _is_openclaw_hermes_handoff_noise_result(result, result_text)
            # Only direct durable rows earn the distinction boost; derived
            # summary/reflection/brain-analysis rows are handled generically by
            # the source-quality prior below (no probe-specific exclusion list).
            if _is_openclaw_hermes_distinction_result(all_tokens, result_text) and not (
                openclaw_setup_noise or openclaw_handoff_noise or live_state_snapshot or low_authority
            ):
                delta += 140.0
                reasons.append("openclaw_hermes_distinction")
            if openclaw_setup_noise:
                delta -= 360.0
                reasons.append("openclaw_setup_noise_penalty")
            if openclaw_handoff_noise:
                delta -= 140.0
                reasons.append("openclaw_distinction_handoff_penalty")

        if codex_hermes_tui_query:
            codex_skill_sync_noise = _is_codex_skill_sync_noise_result(result, result_text)
            if codex_skill_sync_noise:
                delta -= 160.0
                reasons.append("codex_skill_sync_noise_penalty")
            elif _is_codex_hermes_tui_result(all_tokens, result_text):
                delta += 110.0
                reasons.append("codex_hermes_tui_preference")

        if codex_hermes_tui_query and _is_old_claude_code_restriction_noise(all_tokens, result_text):
            delta -= 120.0
            reasons.append("old_claude_code_restriction_penalty")

        if (
            budget_local_cloud_query
            and _is_brain_contract_result(result, result_text)
            and "brain" not in query_tokens
        ):
            delta -= 120.0
            reasons.append("brain_contract_offtopic_penalty")

        primary_calendar_tooling_result = _is_primary_calendar_tooling_result(result, result_text)
        if calendar_tooling_query and primary_calendar_tooling_result:
            delta += 90.0
            reasons.append("primary_tooling_choice")

        if calendar_tooling_query and _is_personal_calendar_instance(result, result_text):
            delta -= 50.0
            reasons.append("personal_instance_penalty")

        if calendar_tooling_query and _is_openclaw_calendar_skill_inventory_result(result, result_text):
            delta -= 220.0
            reasons.append("openclaw_calendar_inventory_penalty")

        if (
            calendar_tooling_query
            and _is_calendar_tooling_offtopic_result(result, result_text)
            and not primary_calendar_tooling_result
        ):
            delta -= 85.0
            reasons.append("calendar_tooling_offtopic_penalty")

        if broad_tool_recommendation_query and _is_distilled_brain_analysis_result(result, result_text):
            delta -= 90.0
            reasons.append("distilled_brain_analysis_penalty")

        if broad_tool_recommendation_query and _is_broad_tool_recommendation_noise_result(
            result, result_text
        ):
            delta -= 95.0
            reasons.append("broad_tool_recommendation_noise_penalty")

        if terminal_telegram_authorization_query and _is_terminal_telegram_authorization_result(
            all_tokens, result_text
        ):
            delta += 180.0
            reasons.append("terminal_telegram_authorization")

        if terminal_telegram_authorization_query and _is_terminal_telegram_authorization_noise_result(
            result, all_tokens, result_text
        ):
            delta -= 110.0
            reasons.append("terminal_telegram_authorization_noise_penalty")

        if _is_generic_summary_result(result):
            if summary_excluded:
                # User explicitly said "summary 말고" / "not the summary" /
                # "without weekly summary" — push generic summary rows below
                # any non-summary candidate regardless of whether one already
                # sits in this fused window.
                delta -= 300.0
                reasons.append("explicit_summary_exclusion_penalty")
            elif positive_summary_intent:
                # User explicitly asked for summary/recap/요약 — generic
                # Summary rows are the requested rows. Do not penalize.
                pass
            elif specific and non_summary_topical_exists:
                penalty = 85.0 if budget_local_cloud_query else 35.0 if total_overlap >= 2 else 60.0
                delta -= penalty
                reasons.append("generic_summary_penalty")

        if delta:
            result["score"] = float(result.get("score") or 0.0) + delta
            result["governance"] = list(dict.fromkeys([*result.get("governance", []), *reasons]))


def _apply_primary_doc_boost_inplace(fused: list[dict]) -> None:
    """Boost any result whose metadata flags primary_doc_lookup=True.

    The active_recall canonical/L0 layer marks results that came from
    the primary-document identity layer with `metadata.primary_doc_lookup`.
    These should win over semantic hits even when the semantic match has
    a higher raw score — the +35 bump is the empirically-derived margin.

    Mutates each result's `score` in place; returns None.
    """
    for r in fused:
        meta = r.get("metadata") or {}
        if meta.get("primary_doc_lookup"):
            r["score"] = float(r.get("score", 0)) + 35.0


def _sort_and_diversify(fused: list[dict], top_window: int) -> list[dict]:
    """Score-desc sort + reranker diversification on the top window.

    Retrieval-quality dedup/noise filtering is handled by
    _apply_retrieval_quality_filter, which is query-aware and shared by
    /recall/v2 and batch recall. Keep this helper focused on stable ordering
    plus source diversification.
    """
    fused.sort(key=lambda r: r.get("score", 0), reverse=True)
    with contextlib.suppress(Exception):
        fused = _rerank.diversify_sources(
            fused, top_window=top_window, max_per_source=2, max_per_collection=None
        )
    return fused


def _result_rrf_id(result: dict) -> str:
    """Stable RRF key for recall/v2 result fusion.

    Canonical and document-like rows should still fuse by normalized path, but
    learned memories frequently share a coarse source/path value (for example
    ``hermes``). For those memory rows, key by the row id first so distinct
    preferences are not collapsed before governance can rank them.
    """
    collection = str(result.get("collection") or "").lower()
    if collection in {"semantic_memory", "experience", "patterns"} and result.get("id"):
        return str(result["id"])
    return str(result.get("path") or result.get("id") or result.get("title") or "")


def _run_rrf_fuse(result_lists: list[list[dict]]) -> tuple[list[dict], int]:
    """RRF-fuse a list of result lists with recall-specific stable keys.

    Returns (fused_results, elapsed_ms). The caller writes
    `timing['rrf_ms'] = elapsed_ms`.
    """
    keyed_lists = [
        [dict(result, _rrf_id=_result_rrf_id(result)) for result in results] for results in result_lists
    ]
    t_rrf = time.time()
    fused = _rrf.rrf_fuse(keyed_lists, id_key="_rrf_id")
    for result in fused:
        result.pop("_rrf_id", None)
    return fused, int((time.time() - t_rrf) * 1000)


def _apply_time_decay(fused: list[dict]) -> tuple[list[dict], int]:
    """Apply exponential time decay to a fused result list.

    Decay multiplies into each result's `score`, which by this point in
    the pipeline is either the raw RRF score (no rerank) or the rerank
    score (after stage-1 + optional stage-2 cross-encoder).

    Returns (decayed_results, elapsed_ms). The caller writes
    `timing['decay_ms'] = elapsed_ms`.
    """
    t_decay = time.time()
    decayed = _time_decay.apply_to_results(fused)
    return decayed, int((time.time() - t_decay) * 1000)


def _run_hyde_pass(
    q: str,
    n: int,
    search_n_mult: int,
    *,
    domain: str | None,
    where: dict | None,
    collections_arg: list[str] | None,
    entity: str | None,
    source_type: str | None,
    include_history: bool,
    include_obsolete: bool,
    as_of: str | None,
) -> tuple[str | None, dict | None, int]:
    """Second-pass HyDE search.

    Generates a hypothetical answer via `_hyde.generate_hypothetical(q)`,
    then runs `search_unified.search_all` with the hypothetical as the
    query text — this changes the vector embedding while keeping the
    original q for trust/freshness scoring (passed as `original_query`).

    Returns `(hypothetical_or_none, payload_or_none, elapsed_ms)`. The
    caller is responsible for:
      - merging hypothetical into the outer `hypothetical` variable
      - appending payload to `all_payloads` for downstream RRF
      - writing `timing['hyde_ms'] = elapsed_ms`

    Swallows any LLM/search exception so a failed HyDE pass never breaks
    the recall — the route still returns the variant-search results.
    """
    t_hyde = time.time()
    hypothetical: str | None = None
    payload: dict | None = None
    try:
        hypothetical = _hyde.generate_hypothetical(q)
        if hypothetical:
            payload = search_unified.search_all(
                hypothetical,
                n * search_n_mult,
                sources=["rag", "canonical", "obsidian"],
                domain=domain,
                original_query=q,
                where=where,
                collections=collections_arg,
                entity=entity,
                explain=False,
                source_type=source_type,
                include_history=include_history,
                include_obsolete=include_obsolete,
                as_of=as_of,
            )
    except Exception:
        pass
    elapsed_ms = int((time.time() - t_hyde) * 1000)
    return hypothetical, payload, elapsed_ms


# ── Routes: recall ──────────────────────────────────────
@router.get("/recall", response_model=RecallResponse, tags=["recall"])
@limiter.limit("3000/minute")  # M7-WS7 + M8 follow-up: read path — same envelope as /recall/v2
def recall(
    request: Request,
    q: str,
    n: int = Query(default=10, ge=1, le=50),
    since: str | None = None,
    until: str | None = None,
    entity: str | None = None,
    collection: str | None = None,
    domain: str | None = None,
    agent: str | None = Query(default=None, max_length=32),
    source_type: str | None = Query(default=None, max_length=32),
    include_history: bool = Query(default=False),
    include_obsolete: bool = Query(default=False),
    as_of: str | None = Query(default=None, max_length=20),
) -> dict:
    """Multi-dimensional in-process search across rag + canonical + obsidian.

    Phase 1 filters:
      include_history — show superseded memories (default: hide)
      include_obsolete — show obsolete tier memories (default: hide)
      as_of=YYYY-MM-DD — temporal replay: memories valid at that date
    """
    if not q.strip():
        raise HTTPException(status_code=400, detail="q parameter required")

    # Semantic similarity cache — only for plain queries (no filters)
    # When filters are present, results differ per filter combo so we skip cache.
    _filter_free = not any(
        (since, until, entity, collection, domain, source_type, include_history, include_obsolete, as_of)
    )
    if _filter_free:
        cached = _recall_emb_cache_lookup(q)
        if cached is not None:
            # Round 10 C1: still reinforce semantic_memory hits even on cache
            # hit — the user is "accessing" those memories regardless of where
            # the response comes from. Fire-and-forget so cache lookups stay fast.
            try:
                cached_results = cached.get("results", []) if isinstance(cached, dict) else []
                if cached_results:
                    from brain_core.memory_lifecycle import reinforce_all_collections
                    from brain_core.search_unified import _search_bg_pool

                    _search_bg_pool.submit(reinforce_all_collections, cached_results)
            except Exception:
                pass
            return cached

    start_dt, end_dt = temporal.parse_range(since, until)
    # ChromaDB 1.4.1 rejects string operands in $gte/$lt; filter Python-side instead.
    where = None
    collections_arg = [collection] if collection else None
    # Widen n when a temporal filter will post-drop rows so we still return ~n.
    search_n = n * 3 if (start_dt or end_dt) else n

    payload = search_unified.search_all(
        q,
        search_n,
        sources=["rag", "canonical", "obsidian"],
        domain=domain,
        original_query=q,
        where=where,
        collections=collections_arg,
        entity=entity,
        explain=False,
        source_type=source_type,
        include_history=include_history,
        include_obsolete=include_obsolete,
        as_of=as_of,
    )
    if (start_dt or end_dt) and isinstance(payload, dict):
        payload["results"] = temporal.filter_by_created_at(payload.get("results", []), start_dt, end_dt)[:n]
    if _filter_free:
        _recall_emb_cache_put(q, payload)

    # Gap logging moved to /recall/v2 handler (2026-04-12): v2 is the hot path
    # (2400+ requests/day vs v1's ~1800, most of v1 are test-harness) and the
    # v1 threshold of max_score<5.0 never fired in practice — scores are clipped
    # to [0,100] with typical relevant hits at 30-80.

    # Round 10 C1: reinforce-on-access (MemoryBank). Fire-and-forget so we
    # don't add latency to /recall. Only reinforces semantic_memory hits in
    # the top-N — they're the only collection with the access_count metadata.
    # The id may live at top-level (rag results) or nested under metadata.id
    # (canonical results) so we check both paths.
    try:
        results_list = payload.get("results", []) if isinstance(payload, dict) else []
        if results_list:
            from brain_core.memory_lifecycle import reinforce_all_collections
            from brain_core.search_unified import _search_bg_pool

            _search_bg_pool.submit(reinforce_all_collections, results_list)
    except Exception:
        pass
    return payload


# ── Recall cache compatibility wrappers ────────────────────────────────────
# Cache state and pure helpers live in brain_core/recall_cache.py. These thin
# wrappers preserve the historical routes.recall private seam and keep tests
# able to monkeypatch routes.recall._get_embedding.


def _recall_emb_cache_lookup(query: str) -> dict | None:
    return _recall_emb_cache_lookup_base(query, get_embedding=_get_embedding)


def _recall_emb_cache_put(query: str, response: dict) -> None:
    _recall_emb_cache_put_base(query, response, get_embedding=_get_embedding)


# ── Routes: recall v2 (HyDE + expand + rerank + time-decay + RRF) ──
_auto_feedback_count = 0
_auto_feedback_hour = 0  # hour (unix ts // 3600) of last reset
_AUTO_FEEDBACK_MAX_PER_HOUR = 100


def _record_auto_feedback(query: str, results: list[dict], agent: str) -> None:
    """Log served-result impressions. Rate-limited.

    2026-04-16 fix: this function used to auto-reinforce every served
    semantic_memory hit (write score=0.7 + fire reinforce_on_access).
    That created a rich-get-richer spiral — Bjork's interference theory
    predicts frequently-retrieved items should dominate further retrieval
    only when they're actually useful, not merely served. Now:
      - impressions are logged as served-without-score (for LtR training)
      - reinforcement is gated to EXPLICIT /recall/feedback signals only
    Net: salience.access_count only bumps on confirmed usefulness.
    """
    global _auto_feedback_count, _auto_feedback_hour
    now = datetime.now(UTC)
    current_hour = int(now.timestamp()) // 3600
    if current_hour != _auto_feedback_hour:
        _auto_feedback_count = 0
        _auto_feedback_hour = current_hour
    if _auto_feedback_count >= _AUTO_FEEDBACK_MAX_PER_HOUR:
        return
    feedback_log = BRAIN_DIR / "logs" / "search-feedback.jsonl"
    feedback_log.parent.mkdir(parents=True, exist_ok=True)
    ts = now.isoformat()
    lines: list[str] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        rid = r.get("id") or r.get("path") or (r.get("metadata") or {}).get("id") or ""
        col = r.get("collection") or ""
        lines.append(
            json.dumps(
                {
                    "query": query[:500],
                    "result_id": rid,
                    "result_source": col,
                    # score=None marks this as an impression, not a reward.
                    # The learning-to-rank pipeline treats impression-only
                    # as an unlabeled observation — does not update trust.
                    "score": None,
                    "served": True,
                    "timestamp": ts,
                    "agent": agent,
                }
            )
        )
    if not lines:
        return
    budget = _AUTO_FEEDBACK_MAX_PER_HOUR - _auto_feedback_count
    lines = lines[:budget]
    try:
        with feedback_log.open("a") as f:
            f.write("\n".join(lines) + "\n")
        _auto_feedback_count += len(lines)
    except Exception:
        pass
    # Reinforcement REMOVED from the served path (see docstring).
    # Explicit reinforcement still happens in POST /recall/feedback.


@router.get("/recall/v2", response_model=RecallV2Response, tags=["recall"])
@limiter.limit("3000/minute")  # M7-WS7 + M8 follow-up: read path is non-LLM-billable (Ollama only).
# Bumped from 600 → 3000 because back-to-back eval (1212 calls/run) was burst-throttling.
def recall_v2(
    request: Request,
    q: str,
    n: int = Query(default=10, ge=1, le=50),
    hyde: bool = False,
    expand: bool = False,
    rerank: bool = True,
    decay: bool = True,
    iterative: bool = False,
    since: str | None = None,
    until: str | None = None,
    entity: str | None = None,
    collection: str | None = None,
    domain: str | None = None,
    agent: str | None = Query(default=None, max_length=32),
    source_type: str | None = Query(default=None, max_length=32),
    include_history: bool = Query(default=False),
    include_obsolete: bool = Query(default=False),
    as_of: str | None = Query(default=None, max_length=20),
    canonical_first: bool = Query(default=False),
    exclude_already_used: bool = Query(default=False),
    background: BackgroundTasks = None,
) -> RecallV2Response:
    """Enhanced recall with HyDE, query expansion, reranking, time decay.

    Query params:
      hyde    = generate a hypothetical answer via Jenna and search with its embedding
      expand  = generate 3 query variants via Jenna, search each, RRF-merge
      rerank  = apply token-overlap reranker (default ON — cheap, always helps)
      decay   = apply exponential time decay per collection (default ON)
      since/until = temporal range (same as /recall)
      entity/collection/domain = filter passthrough
      agent = filter Qdrant-backed memory results by metadata.agent
      source_type = filter personal collection results by type (note|message|event|reminder)
      canonical_first = Karpathy llm-wiki mode — query the canonical truth
          layer only (skips experience/obsidian/semantic_memory). Use when
          you want wiki-as-truth semantics. Fall back to a regular query
          without this flag if canonical is sparse.
    """
    if not q.strip():
        raise HTTPException(status_code=400, detail="q parameter required")

    # Response cache — identical queries within 30s return cached.
    # See _build_recall_v2_cache_key for the session/agent/adapter inclusions
    # (2026-04-16 R-3 + 2026-04-17 LoRA A/B fix).
    cache_key = _build_recall_v2_cache_key(
        request,
        q,
        n,
        hyde=hyde,
        expand=expand,
        rerank=rerank,
        decay=decay,
        iterative=iterative,
        collection=collection,
        domain=domain,
        agent=agent,
        since=since,
        until=until,
        entity=entity,
        source_type=source_type,
        include_history=include_history,
        include_obsolete=include_obsolete,
        as_of=as_of,
        canonical_first=canonical_first,
        exclude_already_used=exclude_already_used,
    )
    cached = _recall_cache_get(cache_key)
    if cached:
        return cached

    t_start = time.time()
    timing: dict[str, Any] = {}

    if _is_live_state_query(q):
        timing["live_state_query"] = True
        timing["total_ms"] = int((time.time() - t_start) * 1000)
        response = RecallV2Response(
            query=q,
            results=[],
            total_candidates=0,
            hyde_used=False,
            hypothetical=None,
            variants=[],
            rerank_applied=rerank,
            time_decay_applied=decay,
            latency_ms=timing["total_ms"],
            timing=timing,
            meta_note="Live-state/status query — use live tools instead of stale memory recall.",
        )
        _recall_cache_put(cache_key, response)
        return response

    search_query = _augment_query_for_recall(q)
    query_tokens_for_governance = _tokenize_recall_text(search_query)
    calendar_tooling_query = _is_calendar_tooling_query(query_tokens_for_governance)
    terminal_telegram_authorization_query = _is_terminal_telegram_authorization_query(
        query_tokens_for_governance
    )
    openclaw_hermes_distinction_query = _is_openclaw_hermes_distinction_query(query_tokens_for_governance)
    codex_hermes_tui_query = _is_codex_hermes_tui_query(query_tokens_for_governance)
    personal_attribute_query = _query_analyzer.personal_attribute_query_binding(q) is not None
    pure_personal_factoid_probe = _is_pure_personal_factoid_probe(q)

    start_dt, end_dt = temporal.parse_range(since, until)
    # ChromaDB 1.4.1 rejects string operands in $gte/$lt; filter Python-side instead.
    where = {"agent": agent} if agent else None
    collections_arg = [collection] if collection else None
    # Widen inner-search n when a temporal filter will post-drop rows.
    search_n_mult = 3 if (start_dt or end_dt) else 2
    inner_search_n = n * search_n_mult
    # Governance-sensitive preference probes often need a slightly deeper
    # candidate pool; otherwise the exact canonical/tooling row can sit just
    # below the small n=5 inner window and never reach reranking.
    governance_sensitive_query = (
        calendar_tooling_query
        or terminal_telegram_authorization_query
        or openclaw_hermes_distinction_query
        or codex_hermes_tui_query
        or _is_budget_local_cloud_query(query_tokens_for_governance)
        or _is_broad_tool_recommendation_query(query_tokens_for_governance)
        or personal_attribute_query
        or pure_personal_factoid_probe
        # Incident/retrospective probes: the authoritative incident_resolution/
        # postmortem row often sits just below the small n=5 inner window
        # (pool miss, not a ranking miss) — same bounded deepening as the
        # other governance-sensitive classes.
        or _query_analyzer.is_incident_retrospective_query(q)
    )
    if governance_sensitive_query:
        inner_floor = (
            80
            if (calendar_tooling_query or openclaw_hermes_distinction_query or codex_hermes_tui_query)
            else 40
        )
        inner_search_n = max(inner_search_n, inner_floor)

    hypothetical: str | None = None
    variants: list[str] = [search_query]

    # Query expansion first — generates variants that downstream HyDE can also use.
    if expand:
        t_expand = time.time()
        try:
            variants = _hyde.expand_query(search_query, max_variants=3)
        except Exception:
            variants = [search_query]
        timing["expansion_ms"] = int((time.time() - t_expand) * 1000)

    if calendar_tooling_query:
        # Deterministic rescue variant for terse Korean calendar/reminder
        # preference prompts. The relevant distilled analysis is phrased as
        # "Apple Calendar/Reminders ... 도구/흐름 ... 선호"; the generic
        # Korean expansion alone over-matches old AGENTS/TOOLS inventories.
        preference_variant = f"{q} Apple Calendar Reminders 도구 흐름 선호"
        if preference_variant not in variants:
            variants.append(preference_variant)

    if pure_personal_factoid_probe:
        # The natural-language scaffolding ("What should I remember about …")
        # makes the raw_events FTS literal-AND match miss a durable hot-path atom
        # that carries ONLY the distinctive terms. Add a focused variant of just
        # the distinctive ASCII tokens (acronyms/years/proper nouns survive
        # cross-language — "OMSCS 2026" for both the EN and KO prompt) so FTS can
        # AND-match the fact. Generic: terms come from the shared factoid analyzer,
        # never a probe string. Skipped when the prompt has no ASCII anchor (a
        # pure-Hangul fact probe), leaving its negative behavior unchanged.
        factoid_terms = _query_analyzer.personal_factoid_query_terms(q)
        ascii_focus = list(
            dict.fromkeys(w for w in re.findall(r"[A-Za-z0-9]+", q) if w.lower() in factoid_terms)
        )
        focused_variant = " ".join(ascii_focus)
        if focused_variant and focused_variant not in variants:
            variants.append(focused_variant)

    if terminal_telegram_authorization_query:
        # Deterministic rescue variant for natural-language authorization
        # prompts ("allowed by Chris?"). The initial expanded query still
        # contains generic Chris/Telegram allowlist terms that can crowd out the
        # source correction; this concise ops-watchdog/false-positive query is
        # the known evidence shape and stays scoped by the intent gate above.
        for authorization_variant in _TERMINAL_AUTHORIZATION_RESCUE_VARIANTS:
            if authorization_variant not in variants:
                variants.append(authorization_variant)

    if codex_hermes_tui_query:
        workflow_variants = (
            "Chris prefers using Codex through Hermes as an interactive terminal-like tmux TUI when quality or steering matters; headless codex exec is only for bounded automation",
            "Codex Hermes interactive tmux TUI preference headless codex exec bounded automation quality steering",
        )
        for workflow_variant in workflow_variants:
            if workflow_variant not in variants:
                variants.append(workflow_variant)

    if openclaw_hermes_distinction_query:
        distinction_variant = "OpenClaw Hermes current runtime historical distinction"
        if distinction_variant not in variants:
            variants.append(distinction_variant)

    # Run recall for each variant in parallel and RRF-fuse.
    t_search = time.time()
    all_payloads: list[dict] = []
    from concurrent.futures import ThreadPoolExecutor as _VariantPool
    from concurrent.futures import as_completed as _as_completed

    _sources = ["canonical"] if canonical_first else ["rag", "canonical", "obsidian"]

    def _run_variant(v_query, *, override_collections: list[str] | None = None):
        return search_unified.search_all(
            v_query,
            inner_search_n,
            sources=_sources,
            domain=domain,
            original_query=q,
            where=where,
            collections=override_collections if override_collections is not None else collections_arg,
            entity=entity,
            explain=False,
            source_type=source_type,
            include_history=include_history,
            include_obsolete=include_obsolete,
            as_of=as_of,
        )

    if len(variants) == 1 or governance_sensitive_query:
        # Governance-sensitive probes add deterministic rescue variants that
        # often hit the same Qdrant collections as the base query. Running
        # those variants concurrently can trip the inner fanout deadline and
        # drop the semantic/preference rows we added the rescue for. Keep the
        # normal fast parallel path for generic expansion, but serialize these
        # small governed variant sets for recall quality.
        for variant in variants:
            with contextlib.suppress(Exception):
                all_payloads.append(_run_variant(variant))
    else:
        with _VariantPool(max_workers=min(len(variants), 4)) as _vpool:
            futures = {_vpool.submit(_run_variant, v): v for v in variants}
            for fut in _as_completed(futures):
                try:
                    all_payloads.append(fut.result())
                except Exception:
                    continue

    if personal_attribute_query and collections_arg is None and not canonical_first:
        # Scoped owner+attribute lookups are personal-memory lookups even when the
        # caller does not pass collection=personal. The generic all-source path can
        # under-serve terse attribute prompts because vector/rerank candidates from
        # obsidian/experience crowd out the personal collection before the
        # identity/attribute guard runs. Add one scoped payload over the durable
        # personal + canonical stores (identity/profile docs live in canonical)
        # so direct attribute facts enter the normal RRF/governance/filter
        # pipeline instead of special-casing answers.
        with contextlib.suppress(Exception):
            all_payloads.append(_run_variant(search_query, override_collections=["personal", "canonical"]))

    timing["search_ms"] = int((time.time() - t_search) * 1000)
    # See _merge_source_timing for the per-source timing aggregation contract
    # (max across variants since sources run in parallel inside each search_all).
    _merge_source_timing(timing, all_payloads)

    # Optionally replace query embedding via HyDE — see _run_hyde_pass docstring
    # for the second-pass semantics.
    if hyde:
        hyde_hypo, hyde_payload, hyde_ms = _run_hyde_pass(
            q,
            n,
            search_n_mult,
            domain=domain,
            where=where,
            collections_arg=collections_arg,
            entity=entity,
            source_type=source_type,
            include_history=include_history,
            include_obsolete=include_obsolete,
            as_of=as_of,
        )
        if hyde_hypo is not None:
            hypothetical = hyde_hypo
        if hyde_payload is not None:
            all_payloads.append(hyde_payload)
        timing["hyde_ms"] = hyde_ms

    # See _apply_temporal_filter_inplace for the ChromaDB 1.4.1 range-filter
    # workaround. No-op when neither bound is set.
    _apply_temporal_filter_inplace(all_payloads, start_dt, end_dt)

    # Merge all result lists via RRF. See _filter_nonempty_result_lists for
    # why empty/missing-results payloads must be dropped before fusion.
    result_lists = _filter_nonempty_result_lists(all_payloads)
    if not result_lists:
        # Empty/missing retrieval still owes a matched high-priority route its
        # durable guarantee_fact (route_guarantees.yaml lines 6-8: inject directly
        # when the route matches and retrieval is missing/under-served). Active
        # recall already does this; mirror it here so /recall/v2 doesn't drop the
        # guarantee on the empty-retrieval path. No route match → the list stays
        # empty → the existing empty response is returned unchanged.
        guarantee_only: list[dict] = []
        _inject_route_guarantee_results(q, guarantee_only)
        timing["total_ms"] = int((time.time() - t_start) * 1000)
        _metrics_buf.record_search_latency(timing["total_ms"], timing)
        response = _build_empty_recall_v2_response(
            q,
            hyde=hyde,
            hypothetical=hypothetical,
            variants=variants,
            expand=expand,
            rerank=rerank,
            decay=decay,
            t_start=t_start,
            timing=timing,
        )
        if guarantee_only:
            response.results = guarantee_only[:n]
            _recall_cache_put(cache_key, response)
        return response

    fused, rrf_ms = _run_rrf_fuse(result_lists)
    timing["rrf_ms"] = rrf_ms

    # Two-stage rerank (2026-04-12): token-overlap (stage 1) +
    # BGE cross-encoder (stage 2, gated on BRAIN_CROSS_ENCODER_ENABLED).
    # See _run_token_rerank / _run_cross_encoder_rerank docstrings.
    if rerank:
        fused, rerank_ms = _run_token_rerank(q, fused)
        timing["rerank_ms"] = rerank_ms

        fused, ce_top_k, ce_ms = _run_cross_encoder_rerank(q, fused)
        if ce_top_k is not None:
            timing["cross_encoder_top_k"] = ce_top_k
        if ce_ms is not None:
            timing["cross_encoder_ms"] = ce_ms

    # Apply time decay AFTER rerank so freshness actually affects the final ordering.
    # See _apply_time_decay docstring for the score-multiplication contract.
    if decay:
        fused, decay_ms = _apply_time_decay(fused)
        timing["decay_ms"] = decay_ms

    _apply_recall_governance_inplace(q, fused)
    _apply_primary_doc_boost_inplace(fused)
    fused = _sort_and_diversify(fused, top_window=n * 2)
    fused = _apply_retrieval_quality_filter(q, fused)
    _inject_route_guarantee_results(q, fused)
    if not canonical_first:
        # canonical_first is a truth-layer-only contract (sources=["canonical"]);
        # the factoid rescue injects raw_events FTS rows, which would leak
        # non-canonical content into a canonical-only response.
        _inject_personal_factoid_answer(q, fused)
    fused = _sort_and_diversify(fused, top_window=n)

    # Phase G3: opt-in graph-constraint exclusion. See
    # _apply_exclude_already_used docstring for the entity-link semantics.
    if exclude_already_used:
        fused, excluded_count, excl_ms = _apply_exclude_already_used(fused)
        timing["exclude_already_used_ms"] = excl_ms
        timing["exclude_already_used_dropped"] = excluded_count

    # Content enrichment — see _apply_content_enrichment_inplace docstring.
    timing["enrich_ms"] = _apply_content_enrichment_inplace(fused, top_n=n)

    # Metacognitive surface — see _apply_metacognitive_surface_inplace docstring.
    timing["metacognition_ms"] = _apply_metacognitive_surface_inplace(fused, top_n=n)

    # Retrieval-induced inhibition logging — see _log_retrieval_inhibition
    # docstring for the winner/loser semantics and bg-pool dispatch contract.
    _log_retrieval_inhibition(fused, q)

    total_candidates = sum(p.get("total_candidates", 0) for p in all_payloads)
    timing["total_ms"] = int((time.time() - t_start) * 1000)
    timing["result_count"] = min(n, len(fused))
    timing["candidate_count"] = total_candidates

    # ── Phase M9: CRAG iterative retrieval (opt-in via ?iterative=true) ──
    # If the caller asked for iterative recall, score the result confidence
    # and trigger one query expansion + retry on low confidence. Capped at
    # 1 retry to bound latency. The retry recurses into recall_v2 with
    # iterative=False so it's a strict single-shot, no infinite loop.
    #
    # M8.4: Adaptive-RAG router can override the caller's iterative flag for
    # SIMPLE queries (where CRAG is pure latency cost with no recall benefit)
    # and for MULTI queries auto-enable CRAG even when the caller didn't ask.
    # Default OFF via BRAIN_ADAPTIVE_RAG env var. When disabled, the caller's
    # explicit `iterative=` param is honored as before.
    # See _decide_use_crag docstring for the adaptive-RAG router behavior.
    use_crag, _ar_reason = _decide_use_crag(q, iterative)
    if _ar_reason is not None:
        timing["adaptive_rag"] = _ar_reason

    if use_crag and fused:

        def _crag_retry(rewritten_q: str):
            return recall_v2(
                request,
                q=rewritten_q,
                n=n,
                hyde=False,
                expand=False,
                rerank=rerank,
                decay=decay,
                iterative=False,
                since=since,
                until=until,
                entity=entity,
                collection=collection,
                domain=domain,
                agent=agent,
                source_type=source_type,
                include_history=include_history,
                include_obsolete=include_obsolete,
                as_of=as_of,
                canonical_first=canonical_first,
                background=background,
            )

        # See _run_crag_retry docstring for the score → rewrite → retry pipeline.
        fused, _crag_ms, _crag_tele, _crag_err = _run_crag_retry(q, n, fused, _crag_retry)
        if _crag_err is not None:
            timing["crag_error"] = _crag_err
        else:
            timing["crag_ms"] = _crag_ms
            timing["crag"] = _crag_tele
            # CRAG retry returns rows ranked for the rewritten query. Re-apply
            # original-query governance so broad tool recommendation and other
            # preference probes keep their noise penalties after retry.
            _apply_recall_governance_inplace(q, fused)
            _apply_primary_doc_boost_inplace(fused)
            fused = _sort_and_diversify(fused, top_window=n)
            fused = _apply_retrieval_quality_filter(q, fused)

    # Parent-child expand — see _apply_parent_child_expand docstring.
    fused = _apply_parent_child_expand(fused)

    # Community summaries — see _inject_community_summaries docstring for the
    # MULTI-class gate and synthetic-row scoring contract.
    fused, _injected = _inject_community_summaries(q, fused)
    if _injected:
        timing["community_summaries_injected"] = _injected

    _metrics_buf.record_search_latency(timing["total_ms"], timing)

    # 2026-04-17 Phase 4: proactive doubt meta-note.
    _meta_note = _build_meta_note(fused[:n])

    response = RecallV2Response(
        query=q,
        results=fused[:n],
        total_candidates=total_candidates,
        hyde_used=hyde and hypothetical is not None,
        hypothetical=hypothetical,
        variants=variants if expand else [],
        rerank_applied=rerank,
        time_decay_applied=decay,
        latency_ms=timing["total_ms"],
        timing=timing,
        meta_note=_meta_note,
    )
    _recall_cache_put(cache_key, response)

    # Gap logging — see _log_recall_gap docstring for the CE-score threshold
    # heuristic and filter-free guard.
    _log_recall_gap(
        q,
        fused,
        n,
        collection=collection,
        domain=domain,
        entity=entity,
        source_type=source_type,
        since=since,
        until=until,
        as_of=as_of,
        include_history=include_history,
        include_obsolete=include_obsolete,
    )

    # Auto-feedback + action-audit dispatch — see
    # _dispatch_post_recall_side_effects docstring for the off-path dispatch
    # contract. Named audit_actor, NOT `agent`: rebinding `agent` here mutated
    # the closure cell the CRAG `_crag_retry` captures, silently replacing the
    # caller's profile filter with the audit actor.
    audit_actor = request.headers.get("x-agent") or request.query_params.get("actor") or "unknown"
    _dispatch_post_recall_side_effects(q, fused, n, audit_actor, background)

    return response


# 2026-04-16 Tier 3 #13: SSE streaming recall — push-based context.
# Clients (brain-ui, agent hooks) can open a persistent connection and
# receive ranked result chunks as each source in search_unified returns,
# rather than waiting for the full RRF+rerank pipeline. Enables
# mid-conversation context injection (proactive brain). The stream emits
# partial source payloads in arrival order, then a final fused top-K,
# then closes.
@router.get("/recall/stream", tags=["recall"])
def recall_stream(
    q: str,
    n: int = Query(default=10, ge=1, le=50),
    agent: str = "unknown",
) -> StreamingResponse:
    """Server-Sent Events stream of recall results.

    Events emitted (all as `event: <name>\\ndata: <json>\\n\\n`):
      - `source` — one per completed source (rag, canonical, obsidian,
        graph, fts, graph_prefetch) with that source's top-k chunk
      - `fused` — final RRF-fused + reranked top-n after all sources
      - `end` — terminator
    """
    if not q or not q.strip():
        raise HTTPException(status_code=400, detail="q required")

    def _gen():
        # 2026-05-20 W2: real per-source SSE. Previous impl threaded a single
        # search_all() call and only emitted one fused event before end —
        # `_run_source` was defined but never invoked, so clients never saw
        # partial source results despite the documented contract. The new
        # iter_search_all_events generator yields ("source", payload) as each
        # source future completes, then ("fused", ...) after RRF+rerank, then
        # ("end", ...). rid is attached to every payload for client tracing.
        rid = get_request_id() or ""
        t_start = time.time()
        from brain_core.search_unified import iter_search_all_events

        try:
            for kind, payload in iter_search_all_events(q, limit=n):
                payload = dict(payload)  # defensive copy — don't mutate caller's dict
                payload.setdefault("rid", rid)
                if kind == "fused":
                    # Cap the streamed result list at n (search_all already
                    # returns >=n during rerank; clients want n-or-fewer).
                    if isinstance(payload.get("results"), list):
                        payload["results"] = payload["results"][:n]
                elif kind == "end":
                    payload.setdefault("latency_ms", int((time.time() - t_start) * 1000))
                line = f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                yield line.encode("utf-8")
                if kind == "end":
                    return
        except Exception as e:
            err = {"error": str(e)[:200], "rid": rid, "latency_ms": int((time.time() - t_start) * 1000)}
            yield f"event: end\ndata: {json.dumps(err, ensure_ascii=False)}\n\n".encode()

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",  # disable nginx buffering
    }
    return StreamingResponse(_gen(), media_type="text/event-stream", headers=headers)


# 2026-04-17 H-3: agent-ergonomic batch endpoints. AI agents (Claude
# Code, Hermes profiles) often fan out N recalls per task. Serial
# round-trips add up fast — a single batch endpoint lets the agent
# submit a list of queries and get a list of results back in one
# HTTP call. 20-query cap per batch to keep per-call latency bounded.
@router.post("/recall/batch", tags=["recall"])
@limiter.limit("300/minute")
def recall_batch(request: Request, req: RecallBatchRequest) -> dict:
    """Batch recall — submit up to 20 queries in one HTTP call.

    Returns `{"results": [{"query": q, "hits": [...]}, ...]}`. Each
    query runs through the full /recall/v2 pipeline (rerank, decay,
    canonical trust override, metacognition enrichment). Queries run
    in parallel via the shared variant pool to minimize latency.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    import search_unified as _su

    out: list[dict] = []

    def _run_one(q: str) -> dict:
        try:
            if _is_live_state_query(q):
                return {
                    "query": q,
                    "hits": [],
                    "meta_note": "Live-state/status query — use live tools instead of stale memory recall.",
                }
            search_q = _augment_query_for_recall(q)
            payload = _su.search_all(search_q, limit=req.n * 2, original_query=q)
            hits = payload.get("results") or []
            _apply_recall_governance_inplace(q, hits)
            hits = _apply_retrieval_quality_filter(q, _sort_and_diversify(hits, top_window=req.n * 2))
            return {"query": q, "hits": hits[: req.n]}
        except Exception as e:
            return {"query": q, "error": str(e)[:200]}

    with ThreadPoolExecutor(max_workers=min(len(req.queries), 8)) as pool:
        futures = {pool.submit(_run_one, q): q for q in req.queries}
        for fut in as_completed(futures):
            try:
                out.append(fut.result())
            except Exception as e:
                out.append({"query": futures[fut], "error": str(e)[:200]})
    return {"results": out, "count": len(out)}


# /agent/heartbeat moved to brain_core/routes/insights.py


# 2026-05-20 W3: cross-agent session search. Thin wrapper around
# raw_events_fts.search with actor/source_type/session_id filters so codex,
# claude code, and openclaw agents can query each other's transcripts via a
# single canonical endpoint. The minimal MCP profile's brain_search(scope=
# "sessions") routes here. Returns the same shape as /recall/v2 results so
# clients can swap endpoints without reshaping payloads.
@router.get("/brain/sessions/search", tags=["recall"])
@limiter.limit("60/minute")
def brain_sessions_search(
    request: Request,
    q: str,
    n: int = Query(default=10, ge=1, le=50),
    filter_actor: str | None = Query(
        default=None,
        description="Filter by raw_events.actor. Named filter_actor (not 'actor') so it "
        "doesn't collide with the audit ?actor= query param the MCP shim appends.",
    ),
    source_type: str | None = Query(
        default=None,
        description="Filter by raw_events.source_type (e.g. agent_session, coding_event)",
    ),
    session_id: str | None = Query(default=None, description="Filter by raw_events.source_ref"),
) -> dict:
    """Cross-agent session FTS5 search across raw_events."""
    if not q or not q.strip():
        raise HTTPException(status_code=400, detail="q required")
    try:
        from raw_events_fts import search as _fts_search

        hits = _fts_search(
            q,
            limit=n,
            actor=filter_actor,
            source_type=source_type,
            session_id=session_id,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("sessions_search", e)) from e
    return {
        "query": q,
        "results": hits,
        "filters": {"actor": filter_actor, "source_type": source_type, "session_id": session_id},
        "count": len(hits),
    }


# 2026-05-20 W3.5 round 2: compound brain ops. Allowlisted op set so clients
# can chain N brain calls in one HTTP request — equivalent of Hermes
# execute_code's "collapse N tool calls into 1 inference" but without turning
# memory into an execution sandbox. Ops are dispatched sequentially in-process
# (NOT via subprocess or eval), so the audit trail is one route hit per
# compound call. Hard cap 10 ops to bound latency + abuse.
_COMPOUND_OP_ALLOWLIST = frozenset({"search", "remember", "correct", "feedback"})


@router.post("/brain/ops/compound", tags=["brain"])
@limiter.limit("30/minute")
def brain_ops_compound(request: Request, req: CompoundRequest) -> dict:
    """Run up to 10 allowlisted brain ops in a single request.

    Returns ``{compound_id, results: [{op, ok, result}, ...]}`` so callers can
    correlate per-op outcomes. Errors in one op do NOT abort the rest — each
    op runs independently and reports ok/result.
    """
    import uuid as _uuid

    actor = (req.actor or "compound").strip() or "compound"
    compound_id = f"compound_{_uuid.uuid4().hex[:12]}"
    results: list[dict] = []

    # All ops route through the SAME endpoints the standalone calls use, so
    # rate-limit + auth + audit semantics are identical — no shortcut path.
    # Use the local HTTP loopback so each op picks up the live FastAPI app's
    # middleware (auth, rate limit, action_audit) instead of bypassing them.
    import json as _json
    import urllib.parse as _ulp
    import urllib.request as _urlreq
    from pathlib import Path as _Path

    secret = ""
    try:
        secret = _Path("~/.brain/credentials/.personal_webhook_secret").expanduser().read_text().strip()
    except Exception:
        secret = ""

    def _http(method: str, path: str, body: dict | None = None) -> dict:
        data = _json.dumps(body).encode() if body else None
        url = f"http://127.0.0.1:8791{path}"
        r = _urlreq.Request(url, data=data, method=method)  # noqa: S310
        if secret:
            r.add_header("Authorization", f"Bearer {secret}")
        if data:
            r.add_header("Content-Type", "application/json")
        r.add_header("x-agent", actor)
        r.add_header("x-compound-id", compound_id)
        try:
            with _urlreq.urlopen(r, timeout=15) as resp:  # noqa: S310
                return _json.loads(resp.read().decode())
        except Exception as exc:
            return {"error": str(exc)[:200]}

    for entry in req.ops:
        op_name = entry.op.strip().lower()
        a = entry.args or {}
        if op_name not in _COMPOUND_OP_ALLOWLIST:
            results.append({"op": op_name, "ok": False, "result": {"error": "op not in allowlist"}})
            continue
        try:
            if op_name == "search":
                q = a.get("query", "")
                n = int(a.get("limit", 5))
                path = f"/recall/v2?q={_ulp.quote(q)}&n={n}"
                if a.get("collection"):
                    path += f"&collection={_ulp.quote(a['collection'])}"
                out = _http("GET", path)
            elif op_name == "remember":
                out = _http(
                    "POST",
                    "/memory",
                    {
                        "content": a.get("content", ""),
                        "category": a.get("kind", "fact"),
                        "agent": actor,
                        "source": f"compound:{compound_id}",
                        "replaces": a.get("replaces") or [],
                        "replaces_reason": a.get("replaces_reason") or "",
                    },
                )
            elif op_name == "correct":
                out = _http(
                    "POST",
                    "/memory",
                    {
                        "content": a.get("correction", ""),
                        "category": a.get("category", "fact"),
                        "agent": actor,
                        "source": f"compound:{compound_id}:correct",
                        "replaces": a.get("wrong_atom_ids") or [],
                        "replaces_reason": a.get("reason") or "user-correction via compound",
                    },
                )
            elif op_name == "feedback":
                target_id = a.get("target_id", "")
                target_type = (a.get("target_type") or "task").lower()
                success = bool(a.get("success"))
                notes = a.get("notes", "")
                if target_type == "decision":
                    out = _http(
                        "POST",
                        f"/brain/decisions/{_ulp.quote(target_id)}/outcome",
                        {
                            "actual_outcome": notes or ("accepted" if success else "rejected"),
                            "outcome_status": "succeeded" if success else "failed",
                            "review_status": "accepted" if success else "needs_review",
                        },
                    )
                elif target_type == "recall":
                    # /recall/feedback expects SearchFeedbackRequest:
                    # {query, result_id, result_source, useful, agent}.
                    # Codex round-4 defect E2: the prior payload used
                    # {recall_id, useful, notes, agent} and 422'd every time.
                    # Map target_id to result_id and use args.query / args.source
                    # when present (else minimal stubs so the schema validates).
                    out = _http(
                        "POST",
                        "/recall/feedback",
                        {
                            "query": (a.get("query") or "compound_recall_feedback")[:500],
                            "result_id": target_id,
                            "result_source": (a.get("result_source") or "")[:64],
                            "useful": success,
                            "agent": actor[:32],
                            "synthetic": bool(not a.get("query")),
                        },
                    )
                else:  # task
                    suffix = "/complete?chris_acked=true" if success else "/reject"
                    out = _http(
                        "POST",
                        f"/brain/tasks/{_ulp.quote(target_id)}{suffix}",
                        {"result": notes, "agent": actor},
                    )
            else:  # pragma: no cover — guarded by allowlist above
                out = {"error": "unreachable"}
            results.append({"op": op_name, "ok": "error" not in out, "result": out})
        except Exception as exc:
            results.append({"op": op_name, "ok": False, "result": {"error": str(exc)[:200]}})

    # 2026-05-20 W3.5 round 3 (codex defect 4): write a single batch row to
    # action_audit so the compound_id is recoverable. Each sub-op still logs
    # separately through its own endpoint middleware; this row provides the
    # back-reference. compound_id is stuffed into session_id (already indexed)
    # so existing audit queries can group ops without a schema migration.
    try:
        from atoms_store import insert_action_audit as _insert_audit

        _insert_audit(
            route="/brain/ops/compound",
            tool="brain_compound",
            actor=actor,
            query_text=json.dumps(
                {
                    "compound_id": compound_id,
                    "ops": [{"op": r["op"], "ok": r["ok"]} for r in results],
                },
                ensure_ascii=False,
            ),
            session_id=compound_id,
        )
    except Exception:
        # Audit is best-effort — never block the response on it.
        pass

    return {
        "compound_id": compound_id,
        "actor": actor,
        "count": len(results),
        "results": results,
    }


@router.post("/recall/feedback", tags=["recall"])
def search_feedback(req: SearchFeedbackRequest):
    """Record user feedback on search results. Reinforces memory via MemRL."""
    try:
        feedback_log = BRAIN_DIR / "logs" / "search-feedback.jsonl"
        feedback_log.parent.mkdir(parents=True, exist_ok=True)
        with feedback_log.open("a") as f:
            f.write(
                json.dumps(
                    {
                        "timestamp": datetime.now(UTC).isoformat(),
                        "query": req.query,
                        "result_id": req.result_id,
                        "source": req.result_source,
                        "useful": req.useful,
                        "agent": req.agent,
                        "synthetic": req.synthetic,
                    }
                )
                + "\n"
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("feedback log write", e))

    # Reinforce memory if it's a semantic_memory result.
    # 2026-04-16 fix: result_id is a raw Chroma UUID, not prefixed with
    # "semantic_memory:" — that check never matched and the reinforcement
    # was dead code. Dispatch based on result_source (the collection name
    # that recall_v2 actually populates at server.py:1489).
    if req.result_id and req.result_source == "semantic_memory":
        try:
            from entity_graph import reinforce_memory

            reinforce_memory(req.result_id, success=req.useful)
        except Exception:
            pass

    # Phase 7: eval auto-growth signal
    proposal_id: str | None = None
    if req.wrong_answer and req.expected:
        try:
            from eval_proposals import insert_proposal

            proposal_id = insert_proposal(
                query=req.query,
                expected=req.expected,
                source_event="recall_feedback",
                confidence=0.7,
            )
        except Exception:
            pass

    return {"status": "recorded", "eval_proposal_id": proposal_id}


# ── Routes: /brain/ingest/image ── moved to brain_core/routes/ingest.py


# ── Routes: /brain/wm/* ── moved to brain_core/routes/wm.py


# ── Routes: /recall/active — per-turn thalamus (v3 plan) ─────────────────
@router.post(
    "/recall/active",
    response_model=RecallActiveResponse,
    tags=["recall"],
)
@limiter.limit("3000/minute")
def recall_active(request: Request, req: RecallActiveRequest) -> dict:
    """Per-turn attention gating. Called from claude_boot.sh (UserPromptSubmit)
    and OpenClaw before_prompt_build plugin on EVERY user turn.

    Returns intent-routed canonical guarantees + semantic hits + proactive
    alerts + doorbell messages, dedup'd against session_context['recall_seen'].

    Fail-open: any internal failure returns degraded=True with empty blocks
    rather than a 500. Hook scripts must never block the user's prompt.
    """
    # 2026-04-17 hook adoption metrics — count per-agent calls so we can see
    # whether OpenClaw's brain-active-recall hook is actually firing across
    # all 5 agents, not just Claude Code. Surfaces in /metrics under
    # hook_adoption. No persistence — in-memory counter, resets on restart.
    # Log-on-first-failure so a structural bug in metrics_buffer surfaces
    # instead of silently losing all hook telemetry.
    global _hook_metrics_warned
    try:
        _metrics_buf.record_hook_call("recall_active", req.agent or "unknown")
    except Exception:
        if not _hook_metrics_warned:
            log.warning("hook metrics recording failed (suppressing further)", exc_info=True)
            _hook_metrics_warned = True
    t0 = time.time()
    result = active_recall.build_injection(
        prompt=req.prompt,
        session_id=req.session_id,
        turn_idx=req.turn_idx,
        agent=req.agent,
        cwd=req.cwd,
        seen_hashes=req.seen_hashes,
    )
    try:
        _metrics_buf.record_hook_latency("recall_active", int((time.time() - t0) * 1000))
    except Exception:
        if not _hook_metrics_warned:
            log.warning("hook latency recording failed (suppressing further)", exc_info=True)
            _hook_metrics_warned = True
    return result


# ── Cache management ──────────────────────────────────
def clear_caches() -> dict:
    return _clear_recall_caches()
