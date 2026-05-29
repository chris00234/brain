"""Unit tests for recall_v2's smaller in-route service helpers.

Companion to test_recall_v2_cache_key.py — each new helper extracted from
the 803-line recall_v2 handler gets pinned here so the next stage of the
refactor verifies no behavior change.
"""

from __future__ import annotations

import sys
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


# ── _merge_source_timing ────────────────────────────────────────────────


def test_merge_source_timing_empty_payloads_leaves_timing_unchanged():
    from routes.recall import _merge_source_timing

    timing: dict = {"search_ms": 42}
    _merge_source_timing(timing, [])
    assert timing == {"search_ms": 42}


def test_merge_source_timing_single_payload_writes_each_source():
    from routes.recall import _merge_source_timing

    timing: dict = {}
    _merge_source_timing(timing, [{"source_timing": {"rag_ms": 30, "canonical_ms": 12}}])
    assert timing == {"rag_ms": 30, "canonical_ms": 12}


def test_merge_source_timing_takes_max_across_payloads():
    """Multiple variants run in parallel — for the same source key, keep
    the SLOWEST since wall-clock for that source is the max across variants.
    This is the core invariant the merger pins."""
    from routes.recall import _merge_source_timing

    timing: dict = {}
    payloads = [
        {"source_timing": {"rag_ms": 30, "canonical_ms": 12}},
        {"source_timing": {"rag_ms": 45, "canonical_ms": 9}},  # rag is slower in 2nd
        {"source_timing": {"rag_ms": 20, "canonical_ms": 25}},  # canonical slower in 3rd
    ]
    _merge_source_timing(timing, payloads)
    assert timing == {"rag_ms": 45, "canonical_ms": 25}


def test_merge_source_timing_payload_without_source_timing_key_is_skipped():
    """search_unified.search_all returns a dict that either has
    source_timing as a dict or omits the key entirely. Both cases must
    leave the timing accumulator unchanged. (A pre-existing edge case is
    `source_timing: None` — that would AttributeError because the prior
    inline code did the same .items() call without a None guard.
    Not patching here to keep the extraction byte-equal to the original.)
    """
    from routes.recall import _merge_source_timing

    timing: dict = {"search_ms": 100}
    _merge_source_timing(timing, [{}, {"results": []}, {"source_timing": {}}])
    assert timing == {"search_ms": 100}


def test_merge_source_timing_preserves_existing_keys_when_new_value_smaller():
    """If timing already has rag_ms=50 and a payload contributes rag_ms=30,
    the existing higher value must be preserved (we keep MAX, not last-write)."""
    from routes.recall import _merge_source_timing

    timing: dict = {"rag_ms": 50}
    _merge_source_timing(timing, [{"source_timing": {"rag_ms": 30}}])
    assert timing == {"rag_ms": 50}


def test_merge_source_timing_overwrites_when_new_value_larger():
    from routes.recall import _merge_source_timing

    timing: dict = {"rag_ms": 10}
    _merge_source_timing(timing, [{"source_timing": {"rag_ms": 100}}])
    assert timing == {"rag_ms": 100}


def test_merge_source_timing_returns_none_and_mutates_in_place():
    from routes.recall import _merge_source_timing

    timing: dict = {}
    result = _merge_source_timing(timing, [{"source_timing": {"x_ms": 1}}])
    assert result is None
    assert timing == {"x_ms": 1}


# ── _apply_temporal_filter_inplace ──────────────────────────────────────


def test_temporal_filter_no_bounds_is_no_op():
    """Neither start_dt nor end_dt set → payloads untouched. This is the
    common case (most queries have no temporal filter)."""
    from routes.recall import _apply_temporal_filter_inplace

    payloads = [
        {"results": [{"id": "1", "created_at": "2026-01-01"}]},
        {"results": [{"id": "2", "created_at": "2026-06-01"}]},
    ]
    snapshot = [dict(p) for p in payloads]
    _apply_temporal_filter_inplace(payloads, None, None)
    assert payloads == snapshot


def test_temporal_filter_only_start_calls_filter_with_start(monkeypatch):
    """When start_dt is set (with end_dt None), the underlying
    temporal.filter_by_created_at is called with both bounds passed through."""
    from datetime import UTC, datetime

    import temporal
    from routes.recall import _apply_temporal_filter_inplace

    captured: list[tuple] = []

    def _fake_filter(rows, start, end):
        captured.append((tuple(r["id"] for r in rows), start, end))
        if start is None:
            return rows
        return [r for r in rows if r.get("created_at", "") >= start.isoformat()]

    monkeypatch.setattr(temporal, "filter_by_created_at", _fake_filter)

    start = datetime(2026, 3, 1, tzinfo=UTC)
    payloads = [
        {"results": [{"id": "old", "created_at": "2026-01-15"}, {"id": "new", "created_at": "2026-04-01"}]},
    ]
    _apply_temporal_filter_inplace(payloads, start, None)
    assert captured, "temporal.filter_by_created_at was not called"
    assert captured[0][1] == start
    assert captured[0][2] is None


def test_temporal_filter_skips_payloads_with_no_results():
    """Payloads with `results` empty or missing must not crash."""
    from datetime import UTC, datetime

    from routes.recall import _apply_temporal_filter_inplace

    start = datetime(2026, 3, 1, tzinfo=UTC)
    payloads = [
        {},
        {"results": []},
        {"results": None},
        {"other_key": 1},
    ]
    snapshot = [dict(p) for p in payloads]
    _apply_temporal_filter_inplace(payloads, start, None)
    assert payloads == snapshot


def test_temporal_filter_handles_non_dict_payload_gracefully():
    """`isinstance(p, dict)` guard — None/strings as payload elements
    must not raise."""
    from datetime import UTC, datetime

    from routes.recall import _apply_temporal_filter_inplace

    start = datetime(2026, 3, 1, tzinfo=UTC)
    payloads = [None, "not-a-dict", 42, {"results": []}]
    _apply_temporal_filter_inplace(payloads, start, None)
    # No crash, no mutation on the non-dict entries
    assert payloads[0] is None
    assert payloads[1] == "not-a-dict"
    assert payloads[2] == 42
    assert payloads[3] == {"results": []}


# ── _filter_nonempty_result_lists ───────────────────────────────────────


def test_filter_nonempty_drops_missing_results_key():
    from routes.recall import _filter_nonempty_result_lists

    payloads = [{"source_timing": {}}, {"results": [{"id": 1}]}]
    out = _filter_nonempty_result_lists(payloads)
    assert out == [[{"id": 1}]]


def test_filter_nonempty_drops_empty_results_list():
    from routes.recall import _filter_nonempty_result_lists

    payloads = [{"results": []}, {"results": [{"id": 1}]}, {"results": []}]
    out = _filter_nonempty_result_lists(payloads)
    assert out == [[{"id": 1}]]


def test_filter_nonempty_preserves_order():
    """Order matters for downstream RRF — the helper must not reorder."""
    from routes.recall import _filter_nonempty_result_lists

    payloads = [
        {"results": [{"id": "a"}]},
        {"results": []},
        {"results": [{"id": "b"}, {"id": "c"}]},
        {"results": [{"id": "d"}]},
    ]
    out = _filter_nonempty_result_lists(payloads)
    assert out == [
        [{"id": "a"}],
        [{"id": "b"}, {"id": "c"}],
        [{"id": "d"}],
    ]


def test_filter_nonempty_all_empty_returns_empty_outer_list():
    """When every payload is empty/missing results, return [] so the route
    can fast-return the empty-RecallV2Response (this is the early-return
    guard before RRF)."""
    from routes.recall import _filter_nonempty_result_lists

    payloads = [{"results": []}, {}, {"results": []}]
    assert _filter_nonempty_result_lists(payloads) == []


def test_filter_nonempty_empty_input_is_empty_output():
    from routes.recall import _filter_nonempty_result_lists

    assert _filter_nonempty_result_lists([]) == []


# ── _build_empty_recall_v2_response ─────────────────────────────────────


def test_empty_response_basic_shape():
    """Default-shaped no-results response — all metadata populated,
    results=[]/total=0, latency derived from t_start."""
    import time

    from routes.recall import _build_empty_recall_v2_response

    t_start = time.time() - 0.05  # 50ms ago
    resp = _build_empty_recall_v2_response(
        "hello",
        hyde=False,
        hypothetical=None,
        variants=["v1", "v2"],
        expand=False,
        rerank=True,
        decay=True,
        t_start=t_start,
        timing={"search_ms": 12},
    )
    assert resp.query == "hello"
    assert resp.results == []
    assert resp.total_candidates == 0
    assert resp.hyde_used is False
    assert resp.hypothetical is None
    # expand=False → variants must NOT leak into the response
    assert resp.variants == []
    assert resp.rerank_applied is True
    assert resp.time_decay_applied is True
    assert resp.latency_ms >= 40  # at least ~50ms elapsed since t_start
    assert resp.timing == {"search_ms": 12}


def test_empty_response_expand_true_passes_variants_through():
    """expand=True surfaces variants to the caller so they can see which
    query expansions ran — useful for debugging zero-result cases."""
    import time

    from routes.recall import _build_empty_recall_v2_response

    resp = _build_empty_recall_v2_response(
        "q",
        hyde=False,
        hypothetical=None,
        variants=["q", "alt1", "alt2"],
        expand=True,
        rerank=True,
        decay=True,
        t_start=time.time(),
        timing={},
    )
    assert resp.variants == ["q", "alt1", "alt2"]


def test_empty_response_hyde_true_surfaces_hypothetical():
    import time

    from routes.recall import _build_empty_recall_v2_response

    resp = _build_empty_recall_v2_response(
        "what is X?",
        hyde=True,
        hypothetical="X is a concept.",
        variants=[],
        expand=False,
        rerank=True,
        decay=True,
        t_start=time.time(),
        timing={},
    )
    assert resp.hyde_used is True
    assert resp.hypothetical == "X is a concept."


def test_empty_response_rerank_decay_flags_round_trip():
    import time

    from routes.recall import _build_empty_recall_v2_response

    resp = _build_empty_recall_v2_response(
        "q",
        hyde=False,
        hypothetical=None,
        variants=[],
        expand=False,
        rerank=False,
        decay=False,
        t_start=time.time(),
        timing={},
    )
    assert resp.rerank_applied is False
    assert resp.time_decay_applied is False


# ── _run_hyde_pass ──────────────────────────────────────────────────────


def _hyde_kwargs():
    return dict(
        domain=None,
        where=None,
        collections_arg=None,
        entity=None,
        source_type=None,
        include_history=False,
        include_obsolete=False,
        as_of=None,
    )


def test_hyde_success_returns_hypothetical_and_payload(monkeypatch):
    """Happy path: generate_hypothetical returns a string, search_all
    returns a payload. Helper returns the triple."""
    import hyde as _hyde
    import search_unified
    from routes.recall import _run_hyde_pass

    monkeypatch.setattr(_hyde, "generate_hypothetical", lambda q: f"HYPO: {q}")
    captured: dict = {}

    def _fake_search(query, n, **kw):
        captured["query"] = query
        captured["n"] = n
        captured["kw"] = kw
        return {"results": [{"id": "h1", "score": 0.9}]}

    monkeypatch.setattr(search_unified, "search_all", _fake_search)

    hypo, payload, ms = _run_hyde_pass("what is x?", 5, 3, **_hyde_kwargs())
    assert hypo == "HYPO: what is x?"
    assert payload == {"results": [{"id": "h1", "score": 0.9}]}
    assert isinstance(ms, int)
    assert ms >= 0
    # Verify the search_all call shape: hypothetical as query, original_query as q,
    # n*search_n_mult inflated count, fixed three-source list
    assert captured["query"] == "HYPO: what is x?"
    assert captured["n"] == 15  # 5 * 3
    assert captured["kw"]["original_query"] == "what is x?"
    assert captured["kw"]["sources"] == ["rag", "canonical", "obsidian"]
    assert captured["kw"]["explain"] is False


def test_hyde_empty_hypothetical_skips_search(monkeypatch):
    """generate_hypothetical returns falsy → don't call search_all,
    payload is None, timing still recorded."""
    import hyde as _hyde
    import search_unified
    from routes.recall import _run_hyde_pass

    monkeypatch.setattr(_hyde, "generate_hypothetical", lambda q: "")
    search_called: list = []
    monkeypatch.setattr(search_unified, "search_all", lambda *a, **k: search_called.append(True) or {})

    hypo, payload, ms = _run_hyde_pass("q", 5, 3, **_hyde_kwargs())
    assert hypo == ""
    assert payload is None
    assert search_called == [], "search_all called even though hypothetical was empty"
    assert isinstance(ms, int)


def test_hyde_generate_exception_returns_none_none(monkeypatch):
    """If generate_hypothetical raises, helper swallows the exception and
    returns (None, None, elapsed_ms). The route then keeps hypothetical
    at its prior (None) value."""
    import hyde as _hyde
    from routes.recall import _run_hyde_pass

    def _boom(q):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(_hyde, "generate_hypothetical", _boom)
    hypo, payload, ms = _run_hyde_pass("q", 5, 3, **_hyde_kwargs())
    assert hypo is None
    assert payload is None
    assert isinstance(ms, int)


def test_hyde_search_exception_returns_hypo_none_payload(monkeypatch):
    """When generate_hypothetical succeeds but search_all raises, we still
    keep the hypothetical text (it might be useful in the response
    meta_note) — payload is None."""
    import hyde as _hyde
    import search_unified
    from routes.recall import _run_hyde_pass

    monkeypatch.setattr(_hyde, "generate_hypothetical", lambda q: "the answer is X")

    def _boom(*a, **k):
        raise RuntimeError("qdrant timeout")

    monkeypatch.setattr(search_unified, "search_all", _boom)

    hypo, payload, ms = _run_hyde_pass("q", 5, 3, **_hyde_kwargs())
    # Original inline code: `hypothetical = _hyde.generate_hypothetical(q)` runs
    # BEFORE search_all. If search throws, hypothetical is already assigned.
    # The except clause swallows but doesn't undo the assignment.
    assert hypo == "the answer is X"
    assert payload is None
    assert isinstance(ms, int)


def test_hyde_kwargs_threaded_into_search(monkeypatch):
    """All keyword filters must be passed through to search_all unchanged."""
    import hyde as _hyde
    import search_unified
    from routes.recall import _run_hyde_pass

    monkeypatch.setattr(_hyde, "generate_hypothetical", lambda q: "hypo")
    captured: dict = {}

    def _fake_search(query, n, **kw):
        captured.update(kw)
        return {"results": []}

    monkeypatch.setattr(search_unified, "search_all", _fake_search)

    _run_hyde_pass(
        "q",
        4,
        2,
        domain="coding",
        where={"k": "v"},
        collections_arg=["canonical"],
        entity="openclaw",
        source_type="note",
        include_history=True,
        include_obsolete=True,
        as_of="2026-01-01",
    )
    assert captured["domain"] == "coding"
    assert captured["where"] == {"k": "v"}
    assert captured["collections"] == ["canonical"]
    assert captured["entity"] == "openclaw"
    assert captured["source_type"] == "note"
    assert captured["include_history"] is True
    assert captured["include_obsolete"] is True
    assert captured["as_of"] == "2026-01-01"


# ── _run_rrf_fuse ───────────────────────────────────────────────────────


def test_rrf_fuse_uses_stable_synthetic_key(monkeypatch):
    """Helper keys RRF on a synthetic per-result key.

    Canonical docs still fuse by path, but semantic memories often share a
    coarse source/path value such as ``hermes``. Keying those rows by path
    collapses distinct memories and can hide the exact preference row behind a
    noisier same-source memory.
    """
    import rrf as _rrf
    from routes.recall import _run_rrf_fuse

    captured: dict = {}

    def _fake_fuse(lists, **kw):
        captured["lists"] = lists
        captured["kw"] = kw
        return [{"id": "fused"}]

    monkeypatch.setattr(_rrf, "rrf_fuse", _fake_fuse)
    fused, ms = _run_rrf_fuse([[{"id": "a", "path": "same"}], [{"id": "b", "path": "same"}]])
    assert fused == [{"id": "fused"}]
    assert captured["kw"] == {"id_key": "_rrf_id"}
    assert [row["_rrf_id"] for rows in captured["lists"] for row in rows] == ["same", "same"]
    assert isinstance(ms, int)
    assert ms >= 0


def test_rrf_fuse_keeps_distinct_semantic_memories_with_same_path():
    from routes.recall import _run_rrf_fuse

    result_lists = [
        [
            {
                "id": "semantic_memory:sync-note",
                "path": "hermes",
                "collection": "semantic_memory",
                "content": "Codex/Claude Code skill sync notes.",
            },
            {
                "id": "semantic_memory:codex-pref",
                "path": "hermes",
                "collection": "semantic_memory",
                "content": "Chris prefers Codex through Hermes interactive tmux TUI.",
            },
        ]
    ]

    fused, _ = _run_rrf_fuse(result_lists)

    assert {row["id"] for row in fused} == {
        "semantic_memory:sync-note",
        "semantic_memory:codex-pref",
    }


def test_rrf_fuse_empty_input_passes_through(monkeypatch):
    """Empty input is passed through to rrf_fuse — the early-return for
    no-results lives upstream in the route, not in this helper."""
    import rrf as _rrf
    from routes.recall import _run_rrf_fuse

    monkeypatch.setattr(_rrf, "rrf_fuse", lambda lists, **kw: [])
    fused, ms = _run_rrf_fuse([])
    assert fused == []
    assert isinstance(ms, int)


# ── _apply_time_decay ───────────────────────────────────────────────────


def test_time_decay_calls_apply_to_results(monkeypatch):
    """Helper must delegate to _time_decay.apply_to_results with the
    fused list as the sole argument."""
    import time_decay as _td
    from routes.recall import _apply_time_decay

    captured: list = []

    def _fake_decay(results):
        captured.append(results)
        # Mimic a real decay: multiply score by 0.9
        return [{**r, "score": r.get("score", 0) * 0.9} for r in results]

    monkeypatch.setattr(_td, "apply_to_results", _fake_decay)
    fused = [{"id": "a", "score": 1.0}, {"id": "b", "score": 2.0}]
    out, ms = _apply_time_decay(fused)
    assert captured == [fused]
    assert out == [{"id": "a", "score": 0.9}, {"id": "b", "score": 1.8}]
    assert isinstance(ms, int)


def test_time_decay_empty_input_returns_empty(monkeypatch):
    import time_decay as _td
    from routes.recall import _apply_time_decay

    monkeypatch.setattr(_td, "apply_to_results", lambda r: r)
    out, ms = _apply_time_decay([])
    assert out == []
    assert isinstance(ms, int)


# ── _apply_primary_doc_boost_inplace ────────────────────────────────────


def test_primary_doc_boost_adds_35_to_flagged_results():
    """Results whose metadata.primary_doc_lookup is truthy get +35 score.
    The +35 margin is empirically derived to outweigh strong semantic hits."""
    from routes.recall import _apply_primary_doc_boost_inplace

    fused = [
        {"id": "primary", "score": 50.0, "metadata": {"primary_doc_lookup": True}},
        {"id": "semantic", "score": 80.0, "metadata": {"primary_doc_lookup": False}},
        {"id": "noflag", "score": 75.0, "metadata": {}},
        {"id": "nometa", "score": 70.0},
    ]
    _apply_primary_doc_boost_inplace(fused)
    assert fused[0]["score"] == 85.0  # 50 + 35
    assert fused[1]["score"] == 80.0  # untouched
    assert fused[2]["score"] == 75.0
    assert fused[3]["score"] == 70.0


def test_primary_doc_boost_treats_missing_score_as_zero():
    """If `score` key is absent, dict.get returns the default 0, then +35
    yields 35. Verifies the boost still fires on flagged results that
    haven't yet been scored by RRF (edge case)."""
    from routes.recall import _apply_primary_doc_boost_inplace

    fused = [{"id": "x", "metadata": {"primary_doc_lookup": True}}]
    _apply_primary_doc_boost_inplace(fused)
    assert fused[0]["score"] == 35.0


def test_primary_doc_boost_empty_input_is_no_op():
    from routes.recall import _apply_primary_doc_boost_inplace

    fused: list[dict] = []
    _apply_primary_doc_boost_inplace(fused)
    assert fused == []


def test_primary_doc_boost_metadata_none_is_treated_as_no_flag():
    """`r.get("metadata") or {}` — metadata explicitly None must not raise
    and must be treated as no flag (no boost)."""
    from routes.recall import _apply_primary_doc_boost_inplace

    fused = [{"id": "x", "score": 10.0, "metadata": None}]
    _apply_primary_doc_boost_inplace(fused)
    assert fused[0]["score"] == 10.0


# ── _sort_and_diversify ─────────────────────────────────────────────────


def test_sort_and_diversify_sorts_by_score_desc(monkeypatch):
    """Highest score wins. diversify_sources is mocked to return its
    input unchanged so we observe pure sort behavior."""
    import rerank as _rerank
    from routes.recall import _sort_and_diversify

    monkeypatch.setattr(_rerank, "diversify_sources", lambda fused, **kw: fused)
    out = _sort_and_diversify(
        [{"id": "low", "score": 1}, {"id": "high", "score": 10}, {"id": "mid", "score": 5}],
        top_window=10,
    )
    assert [r["id"] for r in out] == ["high", "mid", "low"]


def test_sort_and_diversify_calls_diversify_with_correct_kwargs(monkeypatch):
    """diversify_sources must receive top_window=n, max_per_source=2,
    max_per_collection=None — the exact pre-extraction call shape."""
    import rerank as _rerank
    from routes.recall import _sort_and_diversify

    captured: dict = {}

    def _fake_div(fused, **kw):
        captured.update(kw)
        return fused

    monkeypatch.setattr(_rerank, "diversify_sources", _fake_div)
    _sort_and_diversify([{"id": "a", "score": 1}], top_window=7)
    assert captured == {"top_window": 7, "max_per_source": 2, "max_per_collection": None}


def test_sort_and_diversify_swallows_diversify_exception(monkeypatch):
    """If diversify_sources raises, the sorted list still returns
    (contextlib.suppress(Exception) — failed diversification must not
    fail the whole recall)."""
    import rerank as _rerank
    from routes.recall import _sort_and_diversify

    def _boom(*a, **k):
        raise RuntimeError("diversifier crashed")

    monkeypatch.setattr(_rerank, "diversify_sources", _boom)
    fused = [{"id": "a", "score": 5}, {"id": "b", "score": 10}]
    out = _sort_and_diversify(fused, top_window=5)
    # Sort happened before the diversify attempt, so order is sorted
    assert [r["id"] for r in out] == ["b", "a"]


def test_sort_and_diversify_empty_input(monkeypatch):
    import rerank as _rerank
    from routes.recall import _sort_and_diversify

    monkeypatch.setattr(_rerank, "diversify_sources", lambda fused, **kw: fused)
    assert _sort_and_diversify([], top_window=10) == []


# ── _run_token_rerank (stage 1) ─────────────────────────────────────────


def test_token_rerank_delegates_to_rerank_with_top_k_none(monkeypatch):
    """Stage-1 helper must call _rerank.rerank(q, fused, top_k=None) —
    the pre-extraction signature."""
    import rerank as _rerank
    from routes.recall import _run_token_rerank

    captured: dict = {}

    def _fake_rerank(query, fused, top_k=None):
        captured["query"] = query
        captured["fused"] = fused
        captured["top_k"] = top_k
        return [{**r, "rerank_score": r.get("score", 0) * 1.4} for r in fused]

    monkeypatch.setattr(_rerank, "rerank", _fake_rerank)
    fused_in = [{"id": "a", "score": 5}, {"id": "b", "score": 10}]
    fused_out, ms = _run_token_rerank("q", fused_in)

    assert captured["query"] == "q"
    assert captured["top_k"] is None
    # rerank_score copied into score (the post-rerank score-promotion loop)
    assert fused_out[0]["score"] == 7.0  # 5 * 1.4
    assert fused_out[1]["score"] == 14.0  # 10 * 1.4
    assert isinstance(ms, int)


def test_token_rerank_score_promotion_falls_back_to_existing_score(monkeypatch):
    """If a result has no rerank_score, the promotion loop must keep the
    existing score rather than zero it out."""
    import rerank as _rerank
    from routes.recall import _run_token_rerank

    monkeypatch.setattr(_rerank, "rerank", lambda q, fused, top_k: fused)
    out, _ms = _run_token_rerank("q", [{"id": "x", "score": 42}])
    assert out[0]["score"] == 42


def test_token_rerank_empty_input(monkeypatch):
    import rerank as _rerank
    from routes.recall import _run_token_rerank

    monkeypatch.setattr(_rerank, "rerank", lambda q, fused, top_k: fused)
    out, ms = _run_token_rerank("q", [])
    assert out == []
    assert isinstance(ms, int)


# ── _run_cross_encoder_rerank (stage 2) ─────────────────────────────────


def test_cross_encoder_disabled_returns_none_timing(monkeypatch):
    """When BRAIN_CROSS_ENCODER_ENABLED is false, return (fused, None, None).
    The caller knows not to write timing keys."""
    from routes.recall import _run_cross_encoder_rerank

    from brain_core import config as _brain_config

    monkeypatch.setattr(_brain_config, "BRAIN_CROSS_ENCODER_ENABLED", False, raising=False)
    fused_in = [{"id": "a", "score": 5}]
    fused_out, ce_top_k, ce_ms = _run_cross_encoder_rerank("q", fused_in)
    assert fused_out is fused_in
    assert ce_top_k is None
    assert ce_ms is None


def test_cross_encoder_enabled_calls_choose_top_k_and_rerank(monkeypatch):
    """Happy path with CE enabled: choose_cross_encoder_top_k decides the
    window, rerank_with_cross_encoder applies it. Both timing keys
    returned populated."""
    import sys
    import types

    from routes.recall import _run_cross_encoder_rerank

    from brain_core import config as _brain_config

    monkeypatch.setattr(_brain_config, "BRAIN_CROSS_ENCODER_ENABLED", True, raising=False)

    captured: dict = {}

    def _fake_choose(q, fused, default_top_k):
        captured["choose_args"] = (q, fused, default_top_k)
        return 14

    def _fake_rerank(q, fused, top_k):
        captured["rerank_args"] = (q, fused, top_k)
        return [{**r, "score": r.get("score", 0) + 1} for r in fused]

    fake_mod = types.ModuleType("brain_core.cross_encoder_rerank")
    fake_mod.choose_cross_encoder_top_k = _fake_choose
    fake_mod.rerank_with_cross_encoder = _fake_rerank
    monkeypatch.setitem(sys.modules, "brain_core.cross_encoder_rerank", fake_mod)

    fused_in = [{"id": "a", "score": 5}]
    fused_out, ce_top_k, ce_ms = _run_cross_encoder_rerank("q", fused_in)
    assert ce_top_k == 14
    assert ce_ms is not None
    assert isinstance(ce_ms, int)
    assert captured["choose_args"] == ("q", fused_in, 14)
    assert captured["rerank_args"][2] == 14
    assert fused_out[0]["score"] == 6


def test_cross_encoder_exception_falls_back_to_stage_1(monkeypatch):
    """If the cross-encoder import or call raises, return the fused list
    unchanged + (None, None) timing — stage-1 result stands, a warning
    gets logged but the recall succeeds."""
    import sys
    import types

    from routes.recall import _run_cross_encoder_rerank

    from brain_core import config as _brain_config

    monkeypatch.setattr(_brain_config, "BRAIN_CROSS_ENCODER_ENABLED", True, raising=False)

    fake_mod = types.ModuleType("brain_core.cross_encoder_rerank")

    def _boom(*a, **k):
        raise RuntimeError("CE model load failed")

    fake_mod.choose_cross_encoder_top_k = _boom
    fake_mod.rerank_with_cross_encoder = _boom
    monkeypatch.setitem(sys.modules, "brain_core.cross_encoder_rerank", fake_mod)

    fused_in = [{"id": "a", "score": 5}]
    fused_out, ce_top_k, ce_ms = _run_cross_encoder_rerank("q", fused_in)
    assert fused_out is fused_in
    assert ce_top_k is None
    assert ce_ms is None


def test_cross_encoder_config_import_failure_falls_back(monkeypatch):
    """If the config import / getattr raises, treat CE as disabled rather
    than crashing. (ce_enabled=False on except path.)"""
    import sys
    import types

    # Replace brain_core.config with a module whose attribute access raises.
    class _BoomConfig(types.ModuleType):
        def __getattribute__(self, name):
            if name == "BRAIN_CROSS_ENCODER_ENABLED":
                raise RuntimeError("config import broken")
            return types.ModuleType.__getattribute__(self, name)

    fake = _BoomConfig("brain_core.config")
    monkeypatch.setitem(sys.modules, "brain_core.config", fake)

    from routes.recall import _run_cross_encoder_rerank

    fused_in = [{"id": "a", "score": 5}]
    fused_out, ce_top_k, ce_ms = _run_cross_encoder_rerank("q", fused_in)
    assert fused_out is fused_in
    assert ce_top_k is None
    assert ce_ms is None


# ── _apply_exclude_already_used ─────────────────────────────────────────


def test_exclude_already_used_neo4j_failure_returns_unfiltered(monkeypatch):
    """If get_excluded_entities raises, helper swallows + returns (fused, 0, ms)
    — the rest of the recall must not fail when Neo4j is unreachable."""
    import entity_graph
    from routes.recall import _apply_exclude_already_used

    def _boom(subject, relationship):
        raise RuntimeError("neo4j down")

    monkeypatch.setattr(entity_graph, "get_excluded_entities", _boom)
    fused_in = [{"id": "a", "score": 5}]
    fused_out, dropped, ms = _apply_exclude_already_used(fused_in)
    assert fused_out is fused_in
    assert dropped == 0
    assert isinstance(ms, int)


def test_exclude_already_used_empty_exclusion_set_is_passthrough(monkeypatch):
    """When subject has no graph-recorded `uses` relationships, the
    helper short-circuits before opening atoms.db."""
    import entity_graph
    from routes.recall import _apply_exclude_already_used

    monkeypatch.setattr(entity_graph, "get_excluded_entities", lambda s, r: set())
    fused_in = [{"id": "a", "score": 5}]
    fused_out, dropped, ms = _apply_exclude_already_used(fused_in)
    assert fused_out is fused_in
    assert dropped == 0
    assert isinstance(ms, int)


def test_exclude_already_used_default_subject_relationship_args(monkeypatch):
    """Helper defaults to subject='chris', relationship='uses' — verifies
    the contract that recall_v2 callers rely on."""
    import entity_graph
    from routes.recall import _apply_exclude_already_used

    captured: list = []

    def _capture(subject, relationship):
        captured.append((subject, relationship))
        return set()

    monkeypatch.setattr(entity_graph, "get_excluded_entities", _capture)
    _apply_exclude_already_used([{"id": "x", "score": 1}])
    assert captured == [("chris", "uses")]


def test_exclude_already_used_subject_relationship_override(monkeypatch):
    """Override subject/relationship for non-chris callers (future-proof)."""
    import entity_graph
    from routes.recall import _apply_exclude_already_used

    captured: list = []

    def _capture(subject, relationship):
        captured.append((subject, relationship))
        return set()

    monkeypatch.setattr(entity_graph, "get_excluded_entities", _capture)
    _apply_exclude_already_used(
        [{"id": "x", "score": 1}],
        subject="alice",
        relationship="prefers",
    )
    assert captured == [("alice", "prefers")]


def test_exclude_already_used_sql_failure_logs_and_returns_unfiltered(monkeypatch):
    """If the atoms.db SELECT raises, helper swallows the exception, logs
    a warning, and returns (fused unchanged, 0 dropped, ms). The recall
    must succeed even when the entity-link join fails."""
    import entity_graph
    from routes.recall import _apply_exclude_already_used

    monkeypatch.setattr(entity_graph, "get_excluded_entities", lambda s, r: {"react"})

    # Make the atoms_store import explode at the second try/except boundary
    import contextlib

    import atoms_store

    @contextlib.contextmanager
    def _boom_conn(*a, **k):
        raise RuntimeError("atoms.db locked")
        yield  # pragma: no cover

    monkeypatch.setattr(atoms_store, "_conn", _boom_conn)
    fused_in = [{"id": "a", "score": 5}]
    fused_out, dropped, ms = _apply_exclude_already_used(fused_in)
    assert fused_out is fused_in
    assert dropped == 0
    assert isinstance(ms, int)


# ── _apply_content_enrichment_inplace ──────────────────────────────────


def _allow_content_enrichment_tmp(monkeypatch, tmp_path):
    import routes.recall as recall

    monkeypatch.setattr(recall, "_CONTENT_ENRICH_ALLOWED_ROOTS", (tmp_path,))
    return recall


def test_content_enrichment_replaces_with_anchor_window(tmp_path, monkeypatch):
    """When the chunk's anchor (first 120 chars) appears in the file,
    enrichment returns a window centered on it (up to ±500/MAX-500)."""
    recall = _allow_content_enrichment_tmp(monkeypatch, tmp_path)

    p = tmp_path / "note.md"
    body = "HEADER\n\n" + ("ANCHOR" + "x" * 100) + "\n\nMIDDLE\n\nFOOTER"
    p.write_text(body)

    fused = [
        {
            "id": "1",
            "path": str(p),
            "type": "canonical-note",
            "content": "ANCHOR" + "x" * 100,
        }
    ]
    ms = recall._apply_content_enrichment_inplace(fused, top_n=1)
    assert "ANCHOR" in fused[0]["content"]
    assert isinstance(ms, int)


def test_content_enrichment_falls_back_to_file_head_when_anchor_missing(tmp_path, monkeypatch):
    """If the chunk anchor isn't in the file (stale chunks, edits), return
    the first _CONTENT_ENRICH_MAX_FILE_BYTES of the file."""
    recall = _allow_content_enrichment_tmp(monkeypatch, tmp_path)

    p = tmp_path / "note.md"
    p.write_text("STALE FILE HEAD\n\nbody body body")
    fused = [
        {
            "id": "1",
            "path": str(p),
            "type": "canonical-note",
            "content": "MISSING ANCHOR TEXT",
        }
    ]
    recall._apply_content_enrichment_inplace(fused, top_n=1)
    assert fused[0]["content"].startswith("STALE FILE HEAD")


def test_content_enrichment_skips_non_enrichable_types(tmp_path):
    """Result types not in _CONTENT_ENRICHABLE_TYPES must be left alone
    (no file read, no content rewrite). The semantic_memory collection,
    for example, doesn't have a file path the brain owns."""
    from routes.recall import _apply_content_enrichment_inplace

    p = tmp_path / "x.md"
    p.write_text("file content")
    original = "raw semantic chunk"
    fused = [{"id": "1", "path": str(p), "type": "semantic_memory", "content": original}]
    _apply_content_enrichment_inplace(fused, top_n=1)
    assert fused[0]["content"] == original


def test_content_enrichment_dedupes_paths(tmp_path, monkeypatch):
    """If two top-N results share the same path, only the first gets
    enriched — subsequent rows preserve their per-chunk content."""
    recall = _allow_content_enrichment_tmp(monkeypatch, tmp_path)

    p = tmp_path / "shared.md"
    p.write_text("FILE BODY enriched content here")
    fused = [
        {"id": "1", "path": str(p), "type": "canonical-note", "content": "ANCHOR1"},
        {"id": "2", "path": str(p), "type": "canonical-note", "content": "ANCHOR2"},
    ]
    recall._apply_content_enrichment_inplace(fused, top_n=2)
    assert "FILE BODY" in fused[0]["content"]
    assert fused[1]["content"] == "ANCHOR2"  # untouched


def test_content_enrichment_skips_missing_files(tmp_path):
    """A non-existent path must not raise — the row stays at its original
    chunk content."""
    from routes.recall import _apply_content_enrichment_inplace

    fused = [
        {
            "id": "1",
            "path": str(tmp_path / "does_not_exist.md"),
            "type": "canonical-note",
            "content": "kept",
        }
    ]
    _apply_content_enrichment_inplace(fused, top_n=1)
    assert fused[0]["content"] == "kept"


def test_content_enrichment_respects_top_n_cutoff(tmp_path, monkeypatch):
    """Only the first top_n results are considered — extras stay raw."""
    recall = _allow_content_enrichment_tmp(monkeypatch, tmp_path)

    p = tmp_path / "f.md"
    p.write_text("file body text")
    fused = [
        {"id": "1", "path": str(p), "type": "canonical-note", "content": "raw1"},
        {"id": "2", "path": str(tmp_path / "g.md"), "type": "canonical-note", "content": "raw2"},
    ]
    recall._apply_content_enrichment_inplace(fused, top_n=1)
    assert "file body" in fused[0]["content"]
    assert fused[1]["content"] == "raw2"  # outside top_n window


def test_content_enrichment_empty_input_returns_zero_ms():
    from routes.recall import _apply_content_enrichment_inplace

    ms = _apply_content_enrichment_inplace([], top_n=10)
    assert isinstance(ms, int)
    assert ms >= 0


def test_content_enrichment_metadata_type_fallback(tmp_path, monkeypatch):
    """If `r['type']` is missing, fall back to `r['metadata']['type']`
    (some upstream paths put it there)."""
    recall = _allow_content_enrichment_tmp(monkeypatch, tmp_path)

    p = tmp_path / "x.md"
    p.write_text("file body")
    fused = [
        {
            "id": "1",
            "path": str(p),
            "metadata": {"type": "canonical-note"},
            "content": "raw",
        }
    ]
    recall._apply_content_enrichment_inplace(fused, top_n=1)
    assert "file body" in fused[0]["content"]


def test_content_enrichment_rejects_paths_outside_allowed_roots(tmp_path, monkeypatch):
    recall = _allow_content_enrichment_tmp(monkeypatch, tmp_path / "allowed")
    outside = tmp_path / "outside.md"
    outside.write_text("SECRET OUTSIDE ROOT")
    fused = [{"id": "1", "path": str(outside), "type": "canonical-note", "content": "kept"}]

    recall._apply_content_enrichment_inplace(fused, top_n=1)

    assert fused[0]["content"] == "kept"


def test_content_enrichment_rejects_symlink_escape(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("SECRET OUTSIDE ROOT")
    link = allowed / "link.md"
    link.symlink_to(outside)
    recall = _allow_content_enrichment_tmp(monkeypatch, allowed)
    fused = [{"id": "1", "path": str(link), "type": "canonical-note", "content": "kept"}]

    recall._apply_content_enrichment_inplace(fused, top_n=1)

    assert fused[0]["content"] == "kept"


def test_exclude_already_used_no_result_ids_short_circuits(monkeypatch):
    """If fused has results without 'id' keys (or is empty), the SQL
    query is skipped (result_ids and excluded_lower guard)."""
    import entity_graph
    from routes.recall import _apply_exclude_already_used

    monkeypatch.setattr(entity_graph, "get_excluded_entities", lambda s, r: {"react"})

    import atoms_store

    sql_called: list = []
    monkeypatch.setattr(
        atoms_store,
        "_conn",
        lambda *a, **k: sql_called.append(True) or (_ for _ in ()).throw(Exception("guard")),
    )

    # No ids on the results → SQL must not be called
    fused_in = [{"score": 5}, {"score": 1}]
    fused_out, dropped, ms = _apply_exclude_already_used(fused_in)
    assert fused_out is fused_in
    assert dropped == 0
    assert sql_called == [], "atoms_store._conn called when there were no ids"


# ── _apply_metacognitive_surface_inplace ───────────────────────────────


class _FakeAtomsRow(dict):
    """sqlite3.Row stand-in — supports r['col'] access used by the helper."""


class _FakeAtomsCursor:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def fetchall(self) -> list[dict]:
        return [_FakeAtomsRow(r) for r in self._rows]


class _FakeAtomsConn:
    def __init__(self, rows: list[dict]):
        self._rows = rows
        self.last_sql: str | None = None
        self.last_params: list | None = None

    def execute(self, sql: str, params=()):
        self.last_sql = sql
        self.last_params = list(params)
        return _FakeAtomsCursor(self._rows)


class _FakeAtomsConnCtx:
    """Context manager wrapper matching `with _atoms_conn() as c:` shape."""

    def __init__(self, conn: _FakeAtomsConn):
        self._conn = conn

    def __enter__(self) -> _FakeAtomsConn:
        return self._conn

    def __exit__(self, *exc) -> None:
        return None


class _FakeQdrantPoint:
    def __init__(self, payload: dict):
        self.payload = payload


class _FakeVectorStore:
    def __init__(self, points: list[_FakeQdrantPoint] | None = None, raise_exc: Exception | None = None):
        self._points = points or []
        self._raise_exc = raise_exc
        self.last_call: dict | None = None

    def get(self, collection: str, **kwargs):
        self.last_call = {"collection": collection, **kwargs}
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._points


def test_metacognitive_surface_injects_confidence_and_trust(monkeypatch):
    """Pass 1 happy path: semantic_memory rows pick up confidence /
    confidence_raw / trust_score_current from the atoms ledger."""
    import atoms_store
    import confidence_calibration
    from routes import recall as recall_mod
    from routes.recall import _apply_metacognitive_surface_inplace

    conn = _FakeAtomsConn(
        rows=[
            {"chroma_id": "atm_1", "confidence": 0.8, "trust_score": 0.9},
            {"chroma_id": "atm_2", "confidence": 0.2, "trust_score": 0.4},
        ]
    )
    monkeypatch.setattr(atoms_store, "_conn", lambda *a, **k: _FakeAtomsConnCtx(conn))
    monkeypatch.setattr(confidence_calibration, "apply_calibration", lambda x: min(1.0, x + 0.1))
    monkeypatch.setattr(recall_mod, "get_vector_store", lambda: _FakeVectorStore(points=[]))

    fused = [
        {"id": "atm_1", "collection": "semantic_memory"},
        {"id": "atm_2", "collection": "semantic_memory"},
        {"id": "doc_1", "collection": "canonical"},  # untouched
    ]
    ms = _apply_metacognitive_surface_inplace(fused, top_n=3)

    assert isinstance(ms, int) and ms >= 0
    assert fused[0]["confidence"] == 0.9  # 0.8 + 0.1 calibration
    assert fused[0]["confidence_raw"] == 0.8
    assert fused[0]["trust_score_current"] == 0.9
    assert fused[1]["confidence_raw"] == 0.2
    assert fused[1]["trust_score_current"] == 0.4
    # canonical row must NOT pick up confidence — only semantic_memory.
    assert "confidence" not in fused[2]
    assert "trust_score_current" not in fused[2]


def test_metacognitive_surface_skips_when_no_semantic_memory(monkeypatch):
    """If none of the top-N rows are semantic_memory, atoms_store._conn
    must not be opened (sm_ids list is empty)."""
    import atoms_store
    from routes import recall as recall_mod
    from routes.recall import _apply_metacognitive_surface_inplace

    conn_calls: list = []

    def _fake_conn(*a, **k):
        conn_calls.append(True)
        return _FakeAtomsConnCtx(_FakeAtomsConn(rows=[]))

    monkeypatch.setattr(atoms_store, "_conn", _fake_conn)
    monkeypatch.setattr(recall_mod, "get_vector_store", lambda: _FakeVectorStore(points=[]))

    fused = [{"id": "x", "collection": "canonical"}]
    _apply_metacognitive_surface_inplace(fused, top_n=1)
    assert conn_calls == [], "_conn opened with no semantic_memory rows"
    assert "confidence" not in fused[0]


def test_metacognitive_surface_atoms_failure_does_not_crash(monkeypatch):
    """Pass 1 swallowing: if atoms_store._conn raises, the helper must
    still return an int and not raise — Pass 2 still runs."""
    import atoms_store
    from routes import recall as recall_mod
    from routes.recall import _apply_metacognitive_surface_inplace

    def _boom(*a, **k):
        raise RuntimeError("atoms.db unavailable")

    monkeypatch.setattr(atoms_store, "_conn", _boom)
    monkeypatch.setattr(recall_mod, "get_vector_store", lambda: _FakeVectorStore(points=[]))

    fused = [{"id": "atm_1", "collection": "semantic_memory"}]
    ms = _apply_metacognitive_surface_inplace(fused, top_n=1)
    assert isinstance(ms, int)
    assert "confidence" not in fused[0]


def test_metacognitive_surface_calibration_import_failure_falls_back(monkeypatch):
    """If confidence_calibration can't be imported, the helper uses an
    identity calibration — confidence == confidence_raw."""
    import sys as _sys

    import atoms_store
    from routes import recall as recall_mod
    from routes.recall import _apply_metacognitive_surface_inplace

    conn = _FakeAtomsConn(rows=[{"chroma_id": "atm_1", "confidence": 0.7, "trust_score": 0.6}])
    monkeypatch.setattr(atoms_store, "_conn", lambda *a, **k: _FakeAtomsConnCtx(conn))
    monkeypatch.setattr(recall_mod, "get_vector_store", lambda: _FakeVectorStore(points=[]))
    # Remove the cached module + force ImportError on next import.
    monkeypatch.setitem(_sys.modules, "confidence_calibration", None)

    fused = [{"id": "atm_1", "collection": "semantic_memory"}]
    _apply_metacognitive_surface_inplace(fused, top_n=1)
    assert fused[0]["confidence"] == 0.7
    assert fused[0]["confidence_raw"] == 0.7


def test_metacognitive_surface_counts_unresolved_contradictions(monkeypatch):
    """Pass 2 happy path: count unresolved semantic_contradictions rows
    keyed off memory_id_a / memory_id_b. Resolved rows must be ignored."""
    import atoms_store
    from routes import recall as recall_mod
    from routes.recall import _apply_metacognitive_surface_inplace

    monkeypatch.setattr(atoms_store, "_conn", lambda *a, **k: _FakeAtomsConnCtx(_FakeAtomsConn(rows=[])))
    points = [
        _FakeQdrantPoint({"memory_id_a": "atm_1", "memory_id_b": "atm_2", "resolved": False}),
        _FakeQdrantPoint({"memory_id_a": "atm_1", "memory_id_b": "atm_3", "resolved": False}),
        _FakeQdrantPoint({"memory_id_a": "atm_2", "memory_id_b": "atm_1", "resolved": True}),  # skipped
    ]
    fake_vs = _FakeVectorStore(points=points)
    monkeypatch.setattr(recall_mod, "get_vector_store", lambda: fake_vs)

    fused = [
        {"id": "atm_1", "collection": "canonical"},
        {"id": "atm_2", "collection": "canonical"},
        {"id": "atm_3", "collection": "canonical"},
    ]
    _apply_metacognitive_surface_inplace(fused, top_n=3)

    # atm_1 appears in both unresolved rows (a in row1, a in row2) → 2
    assert fused[0]["pending_contradictions"] == 2
    # atm_2 appears only in row1 (b), since row3 was resolved → 1
    assert fused[1]["pending_contradictions"] == 1
    # atm_3 appears only in row2 (b) → 1
    assert fused[2]["pending_contradictions"] == 1

    # Filter shape contract — must request both sides with $or / $in.
    flt = fake_vs.last_call["filter"]
    assert "$or" in flt and len(flt["$or"]) == 2
    assert flt["$or"][0]["memory_id_a"]["$in"] == ["atm_1", "atm_2", "atm_3"]
    assert flt["$or"][1]["memory_id_b"]["$in"] == ["atm_1", "atm_2", "atm_3"]


def test_metacognitive_surface_vector_store_failure_does_not_crash(monkeypatch):
    """Pass 2 swallowing: if get_vector_store().get() raises, the helper
    still returns an int and doesn't surface pending_contradictions."""
    import atoms_store
    from routes import recall as recall_mod
    from routes.recall import _apply_metacognitive_surface_inplace

    monkeypatch.setattr(atoms_store, "_conn", lambda *a, **k: _FakeAtomsConnCtx(_FakeAtomsConn(rows=[])))
    monkeypatch.setattr(
        recall_mod, "get_vector_store", lambda: _FakeVectorStore(raise_exc=RuntimeError("qdrant down"))
    )

    fused = [{"id": "atm_1", "collection": "canonical"}]
    ms = _apply_metacognitive_surface_inplace(fused, top_n=1)
    assert isinstance(ms, int)
    assert "pending_contradictions" not in fused[0]


def test_metacognitive_surface_empty_fused_returns_int(monkeypatch):
    """Empty input still returns an int ms timing and doesn't crash."""
    import atoms_store
    from routes import recall as recall_mod
    from routes.recall import _apply_metacognitive_surface_inplace

    monkeypatch.setattr(atoms_store, "_conn", lambda *a, **k: _FakeAtomsConnCtx(_FakeAtomsConn(rows=[])))
    monkeypatch.setattr(recall_mod, "get_vector_store", lambda: _FakeVectorStore(points=[]))

    ms = _apply_metacognitive_surface_inplace([], top_n=10)
    assert isinstance(ms, int)
    assert ms >= 0


# ── _log_retrieval_inhibition ─────────────────────────────────────────


class _FakeBgPool:
    def __init__(self):
        self.calls: list[tuple] = []

    def submit(self, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))


def test_inhibition_two_semantic_results_dispatches_competition(monkeypatch):
    """At least 2 semantic_memory results in top-5 → bg-pool dispatch with
    winner = top.id, losers = ranks 2..N (capped at 5 input rows)."""
    import sys as _sys
    import types

    from routes.recall import _log_retrieval_inhibition

    bg = _FakeBgPool()
    _stub_ri = types.ModuleType("retrieval_inhibition")
    _stub_ri.log_competition = lambda w, lo, q: ("called", w, lo, q)
    monkeypatch.setitem(_sys.modules, "retrieval_inhibition", _stub_ri)

    _stub_su = types.ModuleType("brain_core.search_unified")
    _stub_su._search_bg_pool = bg
    monkeypatch.setitem(_sys.modules, "brain_core.search_unified", _stub_su)

    fused = [
        {"id": "atm_1", "collection": "semantic_memory"},
        {"id": "atm_2", "collection": "semantic_memory"},
        {"id": "atm_3", "collection": "semantic_memory"},
        {"id": "doc_4", "collection": "canonical"},  # skipped (wrong collection)
        {"id": "atm_5", "collection": "semantic_memory"},
    ]
    _log_retrieval_inhibition(fused, "what is x")

    assert len(bg.calls) == 1
    fn, args, _kw = bg.calls[0]
    assert fn is _stub_ri.log_competition
    winner, losers, q = args
    assert winner == "atm_1"
    assert losers == ["atm_2", "atm_3", "atm_5"]
    assert q == "what is x"


def test_inhibition_fewer_than_two_semantic_results_no_dispatch(monkeypatch):
    """If <2 semantic_memory results in top-5, no bg.submit must happen —
    graph/canonical winners don't generate inhibition signals."""
    import sys as _sys
    import types

    from routes.recall import _log_retrieval_inhibition

    bg = _FakeBgPool()
    _stub_ri = types.ModuleType("retrieval_inhibition")
    _stub_ri.log_competition = lambda *a, **k: None
    _stub_su = types.ModuleType("brain_core.search_unified")
    _stub_su._search_bg_pool = bg
    monkeypatch.setitem(_sys.modules, "retrieval_inhibition", _stub_ri)
    monkeypatch.setitem(_sys.modules, "brain_core.search_unified", _stub_su)

    # Only one semantic_memory hit in top 5 → no competition signal.
    fused = [
        {"id": "doc_1", "collection": "canonical"},
        {"id": "atm_1", "collection": "semantic_memory"},
        {"id": "doc_2", "collection": "canonical"},
    ]
    _log_retrieval_inhibition(fused, "q")
    assert bg.calls == []


def test_inhibition_empty_fused_no_dispatch(monkeypatch):
    import sys as _sys
    import types

    from routes.recall import _log_retrieval_inhibition

    bg = _FakeBgPool()
    _stub_su = types.ModuleType("brain_core.search_unified")
    _stub_su._search_bg_pool = bg
    monkeypatch.setitem(_sys.modules, "brain_core.search_unified", _stub_su)

    _log_retrieval_inhibition([], "q")
    assert bg.calls == []


def test_inhibition_import_failure_swallowed(monkeypatch):
    """If retrieval_inhibition import fails, the helper must not raise."""
    import sys as _sys

    from routes.recall import _log_retrieval_inhibition

    monkeypatch.setitem(_sys.modules, "retrieval_inhibition", None)
    fused = [
        {"id": "atm_1", "collection": "semantic_memory"},
        {"id": "atm_2", "collection": "semantic_memory"},
    ]
    # Must not raise even though the import inside will fail.
    _log_retrieval_inhibition(fused, "q")


# ── _run_crag_retry ──────────────────────────────────────────────────


class _FakeSecondHop:
    def __init__(self, results: list[dict]):
        self.results = results


def _install_crag_stubs(
    monkeypatch,
    *,
    score_return,
    should_iterate: bool,
    expanded_query: str | None = None,
):
    """Stub brain_core.crag with deterministic helpers. score_return may be
    a single _FakeConfidenceReport (used for both first + second hop) or a
    list (first call returns [0], second returns [1])."""
    import sys as _sys
    import types

    stub = types.ModuleType("brain_core.crag")
    if isinstance(score_return, list):
        seq = list(score_return)

        def _score(results, query=None):
            return seq.pop(0) if seq else _FakeConfidenceReport(0.0, {})

        stub.score_confidence = _score
    else:
        stub.score_confidence = lambda results, query=None: score_return
    stub.should_iterate = lambda report: should_iterate
    stub.expand_query = lambda query, top_results: expanded_query or query
    monkeypatch.setitem(_sys.modules, "brain_core.crag", stub)
    # Also disable self_rag so first-hop scoring is byte-equal to the heuristic
    monkeypatch.setitem(_sys.modules, "brain_core.self_rag", None)
    return stub


def test_crag_retry_high_confidence_skips_retry(monkeypatch):
    """When _crag_should_iterate returns False, no retry runs — telemetry
    shows iterated=False and the input fused passes through unchanged."""
    from routes.recall import _run_crag_retry

    _install_crag_stubs(
        monkeypatch,
        score_return=_FakeConfidenceReport(0.9, {"c": "high"}),
        should_iterate=False,
    )

    retry_calls: list = []
    fused_in = [{"id": "doc_1", "score": 90}]
    out, ms, tele, err = _run_crag_retry("q", n=5, fused=fused_in, retry_fn=lambda rq: retry_calls.append(rq))
    assert err is None
    assert out is fused_in
    assert tele["iterated"] is False
    assert tele["first_hop_confidence"] == 0.9
    assert tele["first_hop_components"] == {"c": "high"}
    assert isinstance(ms, int) and ms >= 0
    assert retry_calls == [], "retry_fn called when iterate=False"


def test_crag_retry_iterates_and_second_hop_wins(monkeypatch):
    """should_iterate=True + expand_query returns a new query + second-hop
    score > first-hop → fused replaced with second_hop.results, selected
    = second_hop."""
    from routes.recall import _run_crag_retry

    _install_crag_stubs(
        monkeypatch,
        score_return=[
            _FakeConfidenceReport(0.30, {"first": 1}),  # first-hop
            _FakeConfidenceReport(0.80, {"second": 1}),  # second-hop
        ],
        should_iterate=True,
        expanded_query="rewritten query",
    )

    retry_called_with: list = []

    def _retry(rewritten_q: str):
        retry_called_with.append(rewritten_q)
        return _FakeSecondHop([{"id": "doc_better", "score": 100}])

    fused_in = [{"id": "doc_1", "score": 50}]
    out, _ms, tele, err = _run_crag_retry("orig q", n=5, fused=fused_in, retry_fn=_retry)

    assert err is None
    assert retry_called_with == ["rewritten query"]
    assert tele["iterated"] is True
    assert tele["expanded_query"] == "rewritten query"
    assert tele["second_hop_confidence"] == 0.80
    assert tele["selected"] == "second_hop"
    assert len(out) == 1
    assert out[0]["id"] == "doc_better"


def test_crag_retry_iterates_but_first_hop_wins(monkeypatch):
    """If second_hop score is NOT greater than first_hop, keep the original
    fused; telemetry records selected=first_hop."""
    from routes.recall import _run_crag_retry

    _install_crag_stubs(
        monkeypatch,
        score_return=[
            _FakeConfidenceReport(0.70, {"first": 1}),
            _FakeConfidenceReport(0.40, {"second": 1}),
        ],
        should_iterate=True,
        expanded_query="rewritten",
    )

    fused_in = [{"id": "doc_1", "score": 50}]
    out, _ms, tele, err = _run_crag_retry(
        "q", n=5, fused=fused_in, retry_fn=lambda rq: _FakeSecondHop([{"id": "loser"}])
    )

    assert err is None
    assert out is fused_in  # fused unchanged
    assert tele["iterated"] is True
    assert tele["selected"] == "first_hop"


def test_crag_retry_rewrite_returns_same_query_skips_retry(monkeypatch):
    """If expand_query returns the same string (or empty), no retry runs."""
    from routes.recall import _run_crag_retry

    _install_crag_stubs(
        monkeypatch,
        score_return=_FakeConfidenceReport(0.30, {}),
        should_iterate=True,
        expanded_query="orig q",  # same as input
    )

    retry_calls: list = []
    fused_in = [{"id": "doc_1"}]
    out, _ms, tele, err = _run_crag_retry(
        "orig q", n=5, fused=fused_in, retry_fn=lambda rq: retry_calls.append(rq)
    )
    assert err is None
    assert out is fused_in
    assert tele["iterated"] is False
    assert "expanded_query" not in tele
    assert retry_calls == []


def test_crag_retry_crag_import_failure_returns_error(monkeypatch):
    """If brain_core.crag is unavailable, helper returns
    (fused_unchanged, 0, {}, error_str). Caller writes timing['crag_error']."""
    import sys as _sys

    from routes.recall import _run_crag_retry

    monkeypatch.setitem(_sys.modules, "brain_core.crag", None)

    fused_in = [{"id": "x"}]
    out, ms, tele, err = _run_crag_retry("q", n=5, fused=fused_in, retry_fn=lambda rq: None)
    assert out is fused_in
    assert ms == 0
    assert tele == {}
    assert err is not None
    assert len(err) <= 200


def test_crag_retry_retry_fn_exception_returns_error(monkeypatch):
    """If retry_fn raises during the second hop, the helper catches it,
    returns (fused_unchanged, 0, {}, error_str) — no partial telemetry leak."""
    from routes.recall import _run_crag_retry

    _install_crag_stubs(
        monkeypatch,
        score_return=_FakeConfidenceReport(0.20, {}),
        should_iterate=True,
        expanded_query="new q",
    )

    def _boom(rq):
        raise RuntimeError("recursive recall crashed")

    fused_in = [{"id": "doc_1"}]
    out, ms, tele, err = _run_crag_retry("q", n=5, fused=fused_in, retry_fn=_boom)
    assert out is fused_in
    assert ms == 0
    assert tele == {}
    assert err is not None and "recursive recall crashed" in err


def test_crag_retry_error_str_truncated_to_200(monkeypatch):
    """A very long exception message must be truncated to 200 chars."""
    import sys as _sys

    # Force crag import to fail with a long message
    import types as _types

    from routes.recall import _run_crag_retry

    stub = _types.ModuleType("brain_core.crag")

    def _long_score(*a, **k):
        raise RuntimeError("x" * 1000)

    stub.score_confidence = _long_score
    stub.should_iterate = lambda r: False
    stub.expand_query = lambda *a, **k: "y"
    monkeypatch.setitem(_sys.modules, "brain_core.crag", stub)
    monkeypatch.setitem(_sys.modules, "brain_core.self_rag", None)

    out, _ms, _tele, err = _run_crag_retry("q", n=5, fused=[{"id": "x"}], retry_fn=lambda rq: None)
    assert err is not None
    assert len(err) == 200


def test_crag_retry_passes_top_3_to_expand_query(monkeypatch):
    """expand_query is called with fused[:3] (NOT max(n,5)) so the rewrite
    sees only the strongest signal."""
    import sys as _sys
    import types

    from routes.recall import _run_crag_retry

    captured: list = []

    stub = types.ModuleType("brain_core.crag")
    stub.score_confidence = lambda r, query=None: _FakeConfidenceReport(0.2, {})
    stub.should_iterate = lambda r: True

    def _expand(query, top_results):
        captured.append(list(top_results))
        return "new"

    stub.expand_query = _expand
    monkeypatch.setitem(_sys.modules, "brain_core.crag", stub)
    monkeypatch.setitem(_sys.modules, "brain_core.self_rag", None)

    fused_in = [{"id": str(i)} for i in range(10)]
    _run_crag_retry(
        "q",
        n=5,
        fused=fused_in,
        retry_fn=lambda rq: _FakeSecondHop([{"id": "x"}]),
    )
    # Only the first 3 docs are passed to expand_query
    assert len(captured[0]) == 3
    assert [r["id"] for r in captured[0]] == ["0", "1", "2"]


# ── _decide_use_crag ─────────────────────────────────────────────────


def test_decide_use_crag_router_disabled_returns_caller_flag(monkeypatch):
    """When adaptive_rag is unavailable, helper falls back to caller's
    iterative flag and returns reason=None."""
    import sys as _sys

    from routes.recall import _decide_use_crag

    monkeypatch.setitem(_sys.modules, "brain_core.adaptive_rag", None)

    use, reason = _decide_use_crag("q", True)
    assert use is True
    assert reason is None

    use, reason = _decide_use_crag("q", False)
    assert use is False
    assert reason is None


def test_decide_use_crag_router_overrides_caller(monkeypatch):
    """When the router fires (e.g. SIMPLE override → CRAG off, or MULTI
    auto-on), its (use, reason) replaces the caller flag."""
    import sys as _sys
    import types

    from routes.recall import _decide_use_crag

    stub = types.ModuleType("brain_core.adaptive_rag")
    stub.should_use_crag = lambda q, caller_explicit: (False, "simple-skip")
    monkeypatch.setitem(_sys.modules, "brain_core.adaptive_rag", stub)

    use, reason = _decide_use_crag("simple q", True)
    assert use is False
    assert reason == "simple-skip"

    # And MULTI auto-on, even with caller_explicit=False
    stub.should_use_crag = lambda q, caller_explicit: (True, "multi-auto-on")
    use, reason = _decide_use_crag("compare x and y", False)
    assert use is True
    assert reason == "multi-auto-on"


def test_decide_use_crag_router_exception_falls_back(monkeypatch):
    """If should_use_crag raises, fall back to caller flag with reason=None."""
    import sys as _sys
    import types

    from routes.recall import _decide_use_crag

    stub = types.ModuleType("brain_core.adaptive_rag")

    def _boom(q, caller_explicit):
        raise RuntimeError("router crash")

    stub.should_use_crag = _boom
    monkeypatch.setitem(_sys.modules, "brain_core.adaptive_rag", stub)

    use, reason = _decide_use_crag("q", True)
    assert use is True
    assert reason is None


# ── _score_crag_first_hop ────────────────────────────────────────────


class _FakeConfidenceReport:
    def __init__(self, score: float, components: dict | None = None):
        self.score = score
        self.components = components or {}


class _FakeSelfRagReport:
    def __init__(self, score: float, components: dict):
        self.score = score
        self.components = components


def _stub_crag_score(monkeypatch, report_factory):
    """Install brain_core.crag.score_confidence stub."""
    import sys as _sys
    import types

    stub = types.ModuleType("brain_core.crag")
    stub.score_confidence = report_factory
    monkeypatch.setitem(_sys.modules, "brain_core.crag", stub)
    return stub


def test_crag_first_hop_no_self_rag_returns_raw_report(monkeypatch):
    """If brain_core.self_rag is unavailable, return the raw heuristic
    confidence report unchanged."""
    import sys as _sys

    from routes.recall import _score_crag_first_hop

    _stub_crag_score(monkeypatch, lambda results, query: _FakeConfidenceReport(0.4, {"heuristic": "x"}))
    monkeypatch.setitem(_sys.modules, "brain_core.self_rag", None)

    rep = _score_crag_first_hop("q", [{"id": "a"}], n=5)
    assert rep.score == 0.4
    assert rep.components == {"heuristic": "x"}


def test_crag_first_hop_self_rag_critique_blends_score(monkeypatch):
    """Happy path: self_rag returns source=self_rag → blend fires and
    components pick up self_rag_score + self_rag_components + blended=True."""
    import sys as _sys
    import types

    from routes.recall import _score_crag_first_hop

    _stub_crag_score(monkeypatch, lambda results, query: _FakeConfidenceReport(0.40, {"h": "raw"}))

    stub_sr = types.ModuleType("brain_core.self_rag")
    stub_sr.critique = lambda q, results: _FakeSelfRagReport(0.80, {"source": "self_rag", "x": 1})
    stub_sr.blend_with_heuristic = lambda sr_score, heur_score: round((sr_score + heur_score) / 2, 3)
    monkeypatch.setitem(_sys.modules, "brain_core.self_rag", stub_sr)

    rep = _score_crag_first_hop("q", [{"id": "a"}], n=5)
    assert rep.score == 0.60  # (0.80 + 0.40) / 2
    assert rep.components["self_rag_score"] == 0.80
    assert rep.components["blended"] is True
    assert rep.components["self_rag_components"] == {"source": "self_rag", "x": 1}
    # Original heuristic components preserved (merged with **)
    assert rep.components["h"] == "raw"


def test_crag_first_hop_self_rag_non_self_rag_source_skips_blend(monkeypatch):
    """If self_rag.critique returns a report whose components.source is NOT
    'self_rag' (e.g. fallback path), blending must NOT fire — the heuristic
    score is preserved."""
    import sys as _sys
    import types

    from routes.recall import _score_crag_first_hop

    _stub_crag_score(monkeypatch, lambda r, query: _FakeConfidenceReport(0.40, {"h": "raw"}))

    stub_sr = types.ModuleType("brain_core.self_rag")
    stub_sr.critique = lambda q, r: _FakeSelfRagReport(0.90, {"source": "fallback"})
    stub_sr.blend_with_heuristic = lambda *a, **k: 99.0  # should NEVER be called
    monkeypatch.setitem(_sys.modules, "brain_core.self_rag", stub_sr)

    rep = _score_crag_first_hop("q", [{"id": "a"}], n=5)
    assert rep.score == 0.40  # not blended
    assert "self_rag_score" not in rep.components
    assert "blended" not in rep.components


def test_crag_first_hop_self_rag_failure_swallowed(monkeypatch):
    """A self_rag exception must not propagate — fall back to the raw
    heuristic report."""
    import sys as _sys
    import types

    from routes.recall import _score_crag_first_hop

    _stub_crag_score(monkeypatch, lambda r, query: _FakeConfidenceReport(0.55, {}))

    stub_sr = types.ModuleType("brain_core.self_rag")

    def _boom(*a, **k):
        raise RuntimeError("self_rag dispatch failed")

    stub_sr.critique = _boom
    monkeypatch.setitem(_sys.modules, "brain_core.self_rag", stub_sr)

    rep = _score_crag_first_hop("q", [{"id": "a"}], n=5)
    assert rep.score == 0.55  # unchanged
    assert "self_rag_score" not in rep.components


def test_crag_first_hop_passes_top_max_n_5_to_score(monkeypatch):
    """Confidence is scored on fused[:max(n,5)] — verify the slice cap
    floors at 5 for small n and uses n for large n."""
    from routes.recall import _score_crag_first_hop

    captured: list = []

    def _fake_score(results, query):
        captured.append((list(results), query))
        return _FakeConfidenceReport(0.5, {})

    _stub_crag_score(monkeypatch, _fake_score)
    import sys as _sys

    monkeypatch.setitem(_sys.modules, "brain_core.self_rag", None)

    fused = [{"id": str(i)} for i in range(20)]
    # n=2 → uses 5
    _score_crag_first_hop("q", fused, n=2)
    assert len(captured[0][0]) == 5
    # n=10 → uses 10
    _score_crag_first_hop("q", fused, n=10)
    assert len(captured[1][0]) == 10
    # query passed through
    assert captured[0][1] == "q"


# ── _apply_parent_child_expand ───────────────────────────────────────


def test_parent_child_expand_delegates_to_module(monkeypatch):
    """When parent_child_expand.expand_to_parents is available, the helper
    returns its output verbatim."""
    import sys as _sys
    import types

    from routes.recall import _apply_parent_child_expand

    stub = types.ModuleType("brain_core.parent_child_expand")
    stub.expand_to_parents = lambda f: [{"id": "expanded"}, *f]
    monkeypatch.setitem(_sys.modules, "brain_core.parent_child_expand", stub)

    out = _apply_parent_child_expand([{"id": "child"}])
    assert out == [{"id": "expanded"}, {"id": "child"}]


def test_parent_child_expand_import_failure_returns_input(monkeypatch):
    """If parent_child_expand can't be imported, fused passes through
    unchanged and the helper does not raise."""
    import sys as _sys

    from routes.recall import _apply_parent_child_expand

    monkeypatch.setitem(_sys.modules, "brain_core.parent_child_expand", None)
    fused_in = [{"id": "x"}]
    out = _apply_parent_child_expand(fused_in)
    assert out is fused_in


def test_parent_child_expand_runtime_failure_returns_input(monkeypatch):
    """A runtime error inside expand_to_parents must not propagate."""
    import sys as _sys
    import types

    from routes.recall import _apply_parent_child_expand

    stub = types.ModuleType("brain_core.parent_child_expand")

    def _boom(f):
        raise RuntimeError("expand crash")

    stub.expand_to_parents = _boom
    monkeypatch.setitem(_sys.modules, "brain_core.parent_child_expand", stub)

    fused_in = [{"id": "x"}]
    out = _apply_parent_child_expand(fused_in)
    assert out is fused_in


# ── _inject_community_summaries ──────────────────────────────────────


class _FakeClassification:
    def __init__(self, label: str):
        self.label = label


def _stub_adaptive_and_communities(monkeypatch, label: str, summaries: list[dict] | None):
    """Install stubs for adaptive_rag.classify and
    community_summaries.get_summaries_matching."""
    import sys as _sys
    import types

    stub_ar = types.ModuleType("brain_core.adaptive_rag")
    stub_ar.classify = lambda q: _FakeClassification(label)
    monkeypatch.setitem(_sys.modules, "brain_core.adaptive_rag", stub_ar)

    stub_cs = types.ModuleType("brain_core.community_summaries")
    stub_cs.get_summaries_matching = lambda q, limit=2: summaries or []
    monkeypatch.setitem(_sys.modules, "brain_core.community_summaries", stub_cs)


def test_community_summaries_non_multi_query_is_noop(monkeypatch):
    """Non-MULTI queries skip injection — fused is returned as-is, count=0."""
    from routes.recall import _inject_community_summaries

    _stub_adaptive_and_communities(monkeypatch, "simple", [{"entities": ["x"], "summary": "y"}])
    fused_in = [{"id": "doc_1", "score": 10}]
    out, injected = _inject_community_summaries("compare x and y", fused_in)
    assert out is fused_in
    assert injected == 0


def test_community_summaries_multi_no_matches_is_noop(monkeypatch):
    """MULTI query but get_summaries_matching returns [] → no injection."""
    from routes.recall import _inject_community_summaries

    _stub_adaptive_and_communities(monkeypatch, "multi", [])
    fused_in = [{"id": "doc_1", "score": 10}]
    out, injected = _inject_community_summaries("q", fused_in)
    assert out is fused_in
    assert injected == 0


def test_community_summaries_multi_match_injects_synthetic(monkeypatch):
    """MULTI + matching summaries → synthetic rows merged into fused with
    score = 0.85 * top_score, clamped to [55, 100]."""
    from routes.recall import _inject_community_summaries

    summaries = [
        {
            "entities": ["openclaw", "brain", "jenna"],
            "summary": "OpenClaw is a multi-agent system",
            "atom_count": 12,
            "generated_at": "2026-04-20T00:00:00Z",
        }
    ]
    _stub_adaptive_and_communities(monkeypatch, "multi", summaries)

    fused_in = [{"id": "doc_1", "score": 100.0}, {"id": "doc_2", "score": 90.0}]
    out, injected = _inject_community_summaries("compare openclaw and brain", fused_in)
    assert injected == 1
    assert len(out) == 3
    # Synthetic row gets score = 100 * 0.85 = 85.0
    synth = next(r for r in out if r.get("collection") == "community_summaries")
    assert synth["score"] == 85.0
    assert synth["source_type"] == "community"
    assert synth["trust_tier"] == 2
    assert synth["title"].startswith("Community: ")
    assert synth["path"].startswith("graph/community/")
    assert synth["metadata"]["entities"] == ["openclaw", "brain", "jenna"]
    assert synth["metadata"]["atom_count"] == 12
    # Top result still leads (top score 100 > synthetic 85)
    assert out[0]["id"] == "doc_1"
    # Synthetic ranks ahead of doc_2 (85 > 80? no, 85 > 90? no — doc_2 is 90 so doc_2 leads)
    # Actually with doc_2=90, synth=85: order is doc_1=100, doc_2=90, synth=85
    assert out[1]["id"] == "doc_2"
    assert out[2]["collection"] == "community_summaries"


def test_community_summaries_score_clamped_to_55_floor(monkeypatch):
    """Very-low top_score → synth_score clamped to 55.0 minimum (when top
    score is non-zero)."""
    from routes.recall import _inject_community_summaries

    _stub_adaptive_and_communities(
        monkeypatch,
        "multi",
        [{"entities": ["x"], "summary": "s", "generated_at": ""}],
    )
    # top_score = 10 → 10*0.85=8.5, clamped UP to 55
    fused_in = [{"id": "doc_1", "score": 10.0}]
    out, _ = _inject_community_summaries("q", fused_in)
    synth = next(r for r in out if r.get("collection") == "community_summaries")
    assert synth["score"] == 55.0


def test_community_summaries_empty_fused_uses_70_score(monkeypatch):
    """When fused is empty (no top_score signal), synth_score defaults to 70.0."""
    from routes.recall import _inject_community_summaries

    _stub_adaptive_and_communities(
        monkeypatch,
        "multi",
        [{"entities": ["x"], "summary": "s", "generated_at": ""}],
    )
    out, injected = _inject_community_summaries("q", [])
    assert injected == 1
    synth = out[0]
    assert synth["score"] == 70.0


def test_community_summaries_classify_failure_returns_unchanged(monkeypatch):
    """If adaptive_rag.classify raises, fall back to (fused, 0) — no crash,
    no warning that breaks the request."""
    import sys as _sys
    import types

    from routes.recall import _inject_community_summaries

    stub_ar = types.ModuleType("brain_core.adaptive_rag")

    def _boom(q):
        raise RuntimeError("adaptive_rag down")

    stub_ar.classify = _boom
    monkeypatch.setitem(_sys.modules, "brain_core.adaptive_rag", stub_ar)

    fused_in = [{"id": "x", "score": 5}]
    out, injected = _inject_community_summaries("q", fused_in)
    assert out is fused_in
    assert injected == 0


def test_community_summaries_id_truncated_to_64(monkeypatch):
    """Synthetic id `community:<joined entities>` is truncated to 64 chars
    on the joined-entities slice to avoid runaway audit row sizes."""
    from routes.recall import _inject_community_summaries

    long_entities = ["x" * 100, "y" * 100, "z" * 100]
    _stub_adaptive_and_communities(
        monkeypatch,
        "multi",
        [{"entities": long_entities, "summary": "s", "generated_at": ""}],
    )
    out, _ = _inject_community_summaries("q", [{"id": "doc", "score": 100}])
    synth = next(r for r in out if r.get("collection") == "community_summaries")
    # Inner truncation: ','.join(s['entities'][:3])[:64] keeps entity-portion ≤ 64
    body_after_prefix = synth["id"][len("community:") :]
    assert len(body_after_prefix) == 64


# ── _to_dashed_uuid / _post_recall_side_effects / _dispatch_post_recall_side_effects ──


def test_to_dashed_uuid_converts_hex32():
    from routes.recall import _to_dashed_uuid

    raw = "0123456789abcdef0123456789abcdef"  # 32 hex chars, no dashes
    out = _to_dashed_uuid(raw)
    assert out == "01234567-89ab-cdef-0123-456789abcdef"


def test_to_dashed_uuid_passes_through_other_shapes():
    from routes.recall import _to_dashed_uuid

    # Already-dashed UUID passes through
    dashed = "01234567-89ab-cdef-0123-456789abcdef"
    assert _to_dashed_uuid(dashed) == dashed

    # Non-hex string passes through
    assert _to_dashed_uuid("not_a_uuid") == "not_a_uuid"

    # Hex but wrong length passes through
    assert _to_dashed_uuid("abc123") == "abc123"

    # Empty / falsy passes through
    assert _to_dashed_uuid("") == ""


def test_post_recall_side_effects_calls_feedback_and_audit(monkeypatch):
    """Happy path: _record_auto_feedback fires, insert_action_audit fires
    with normalized retrieved_chroma_ids and capped at 20."""
    from routes import recall as recall_mod
    from routes.recall import _post_recall_side_effects

    feedback_calls: list = []
    monkeypatch.setattr(
        recall_mod,
        "_record_auto_feedback",
        lambda q, results, agent: feedback_calls.append((q, results, agent)),
    )

    audit_calls: list = []
    import sys as _sys
    import types

    stub_atoms = types.ModuleType("brain_core.atoms_store")
    stub_atoms.insert_action_audit = lambda **kw: audit_calls.append(kw)
    monkeypatch.setitem(_sys.modules, "brain_core.atoms_store", stub_atoms)

    hex32 = "0123456789abcdef0123456789abcdef"
    fused = [
        {"id": hex32, "score": 0.9},
        {"chroma_id": "already-dashed-uuid-id-2"},
        # Out-of-window rows (n=2):
        {"id": "dropped_1"},
    ]
    _post_recall_side_effects("hello", fused, n=2, agent="jenna")

    assert len(feedback_calls) == 1
    assert feedback_calls[0][0] == "hello"
    assert feedback_calls[0][2] == "jenna"
    assert len(feedback_calls[0][1]) == 2  # sliced fused[:n]

    assert len(audit_calls) == 1
    kw = audit_calls[0]
    assert kw["route"] == "/recall/v2"
    assert kw["tool"] == "brain_recall"
    assert kw["actor"] == "jenna"
    assert kw["query_text"] == "hello"
    assert kw["retrieved_chroma_ids"][0] == "01234567-89ab-cdef-0123-456789abcdef"
    assert kw["retrieved_chroma_ids"][1] == "already-dashed-uuid-id-2"
    assert len(kw["retrieved_chroma_ids"]) == 2  # respects n=2 slice


def test_post_recall_side_effects_audit_failure_does_not_kill_feedback(monkeypatch):
    """insert_action_audit failure must not prevent _record_auto_feedback
    (feedback runs FIRST, then audit). If audit fails, swallow."""
    from routes import recall as recall_mod
    from routes.recall import _post_recall_side_effects

    feedback_calls: list = []
    monkeypatch.setattr(
        recall_mod, "_record_auto_feedback", lambda q, results, agent: feedback_calls.append(True)
    )

    import sys as _sys
    import types

    def _boom(**kw):
        raise RuntimeError("audit db down")

    stub_atoms = types.ModuleType("brain_core.atoms_store")
    stub_atoms.insert_action_audit = _boom
    monkeypatch.setitem(_sys.modules, "brain_core.atoms_store", stub_atoms)

    _post_recall_side_effects("q", [{"id": "x"}], n=1, agent="claude")
    assert feedback_calls == [True], "feedback was skipped when audit raised"


def test_post_recall_side_effects_caps_retrieved_ids_at_20(monkeypatch):
    """Audit retrieved_chroma_ids list is capped at 20 even when n > 20."""
    from routes import recall as recall_mod
    from routes.recall import _post_recall_side_effects

    monkeypatch.setattr(recall_mod, "_record_auto_feedback", lambda *a, **k: None)

    captured: list = []
    import sys as _sys
    import types

    stub_atoms = types.ModuleType("brain_core.atoms_store")
    stub_atoms.insert_action_audit = lambda **kw: captured.append(kw)
    monkeypatch.setitem(_sys.modules, "brain_core.atoms_store", stub_atoms)

    fused = [{"id": f"atm_{i}"} for i in range(50)]
    _post_recall_side_effects("q", fused, n=50, agent="claude")
    assert len(captured[0]["retrieved_chroma_ids"]) == 20


def test_post_recall_side_effects_truncates_query_to_500(monkeypatch):
    from routes import recall as recall_mod
    from routes.recall import _post_recall_side_effects

    monkeypatch.setattr(recall_mod, "_record_auto_feedback", lambda *a, **k: None)

    captured: list = []
    import sys as _sys
    import types

    stub_atoms = types.ModuleType("brain_core.atoms_store")
    stub_atoms.insert_action_audit = lambda **kw: captured.append(kw)
    monkeypatch.setitem(_sys.modules, "brain_core.atoms_store", stub_atoms)

    long_q = "x" * 1000
    _post_recall_side_effects(long_q, [{"id": "x"}], n=1, agent="claude")
    assert len(captured[0]["query_text"]) == 500


def test_dispatch_post_recall_uses_background_when_provided(monkeypatch):
    """When FastAPI BackgroundTasks is present, dispatch must use add_task
    and never touch the search bg pool."""
    from routes.recall import _dispatch_post_recall_side_effects

    class _FakeBackground:
        def __init__(self):
            self.tasks: list = []

        def add_task(self, fn, *args, **kwargs):
            self.tasks.append((fn, args, kwargs))

    bg = _FakeBackground()
    # Sentinel: if the search bg pool path is touched, raise loudly.
    import sys as _sys
    import types

    stub_su = types.ModuleType("brain_core.search_unified")

    def _boom(*a, **k):
        raise AssertionError("should not have hit search bg pool")

    stub_su._search_bg_pool = types.SimpleNamespace(submit=_boom)
    monkeypatch.setitem(_sys.modules, "brain_core.search_unified", stub_su)

    fused = [{"id": "x"}]
    _dispatch_post_recall_side_effects("q", fused, 1, "claude", bg)

    assert len(bg.tasks) == 1
    fn, args, _ = bg.tasks[0]
    from routes.recall import _post_recall_side_effects as _expected

    assert fn is _expected
    assert args == ("q", fused, 1, "claude")


def test_dispatch_post_recall_falls_back_to_bg_pool_when_no_background(monkeypatch):
    """When BackgroundTasks is None, submit to search bg pool instead."""
    from routes.recall import _dispatch_post_recall_side_effects

    submits: list = []

    import sys as _sys
    import types

    stub_su = types.ModuleType("brain_core.search_unified")
    stub_su._search_bg_pool = types.SimpleNamespace(
        submit=lambda fn, *args, **kwargs: submits.append((fn, args, kwargs))
    )
    monkeypatch.setitem(_sys.modules, "brain_core.search_unified", stub_su)

    fused = [{"id": "x"}]
    _dispatch_post_recall_side_effects("q", fused, 1, "claude", None)

    assert len(submits) == 1
    fn, args, _ = submits[0]
    from routes.recall import _post_recall_side_effects as _expected

    assert fn is _expected
    assert args == ("q", fused, 1, "claude")


def test_dispatch_post_recall_swallows_bg_pool_failure(monkeypatch):
    """If the search bg pool import or submit fails, dispatch must not raise."""
    import sys as _sys

    from routes.recall import _dispatch_post_recall_side_effects

    monkeypatch.setitem(_sys.modules, "brain_core.search_unified", None)
    # Must not raise — exception is swallowed.
    _dispatch_post_recall_side_effects("q", [{"id": "x"}], 1, "claude", None)


# ── _log_recall_gap ───────────────────────────────────────────────────


def _gap_default_kwargs():
    return dict(
        collection=None,
        domain=None,
        entity=None,
        source_type=None,
        since=None,
        until=None,
        as_of=None,
        include_history=False,
        include_obsolete=False,
    )


def _read_gap_log(monkeypatch, tmp_path):
    """Point BRAIN_DIR at tmp_path so the gap log file lands in the
    test sandbox, and return a reader for its lines."""
    import routes.recall as recall_mod

    monkeypatch.setattr(recall_mod, "BRAIN_DIR", tmp_path)
    return tmp_path / "logs" / "recall-gaps.jsonl"


def test_gap_log_low_ce_score_writes_jsonl_line(monkeypatch, tmp_path):
    """CE scores present but max < 0.52 → gap is logged."""
    import json as _json

    from routes.recall import _log_recall_gap

    gap_path = _read_gap_log(monkeypatch, tmp_path)
    fused = [
        {"id": "1", "cross_encoder_score": 0.30, "score": 50},
        {"id": "2", "cross_encoder_score": 0.40, "score": 40},
    ]
    _log_recall_gap("missing thing", fused, n=10, **_gap_default_kwargs())
    assert gap_path.exists(), "gap log file was not created"
    line = gap_path.read_text().strip()
    record = _json.loads(line)
    assert record["query"] == "missing thing"
    assert record["max_ce_score"] == 0.40
    assert record["n_results"] == 2
    assert record["endpoint"] == "/recall/v2"


def test_gap_log_high_ce_score_does_not_log(monkeypatch, tmp_path):
    """When max CE score ≥ 0.52, no gap line is appended."""
    from routes.recall import _log_recall_gap

    gap_path = _read_gap_log(monkeypatch, tmp_path)
    fused = [{"id": "1", "cross_encoder_score": 0.70, "score": 50}]
    _log_recall_gap("good query", fused, n=10, **_gap_default_kwargs())
    assert not gap_path.exists()


def test_gap_log_no_ce_falls_back_to_blended_score(monkeypatch, tmp_path):
    """If CE wasn't run, fall back to blended score threshold (< 30 = gap)."""
    from routes.recall import _log_recall_gap

    gap_path = _read_gap_log(monkeypatch, tmp_path)
    # No cross_encoder_score keys anywhere; blended score 20 < 30 → gap
    fused = [{"id": "1", "score": 20}]
    _log_recall_gap("flat", fused, n=10, **_gap_default_kwargs())
    assert gap_path.exists()


def test_gap_log_no_ce_high_blended_score_does_not_log(monkeypatch, tmp_path):
    from routes.recall import _log_recall_gap

    gap_path = _read_gap_log(monkeypatch, tmp_path)
    fused = [{"id": "1", "score": 50}]  # 50 ≥ 30 → not a gap
    _log_recall_gap("decent", fused, n=10, **_gap_default_kwargs())
    assert not gap_path.exists()


def test_gap_log_empty_results_is_logged(monkeypatch, tmp_path):
    """Zero-result query is ALWAYS a gap (filter-free check still applies)."""
    from routes.recall import _log_recall_gap

    gap_path = _read_gap_log(monkeypatch, tmp_path)
    _log_recall_gap("nothing here", [], n=10, **_gap_default_kwargs())
    assert gap_path.exists()


def test_gap_log_filtered_query_skipped(monkeypatch, tmp_path):
    """Filtered queries (with collection/domain/entity/etc.) skip gap log —
    a filter producing 0 results is usually intentional, not a brain gap."""
    from routes.recall import _log_recall_gap

    gap_path = _read_gap_log(monkeypatch, tmp_path)
    kw = _gap_default_kwargs()
    kw["collection"] = "canonical"
    _log_recall_gap("scoped", [], n=10, **kw)
    assert not gap_path.exists()

    # Same for each individual gate.
    gap_path.unlink(missing_ok=True)
    for k in ("domain", "entity", "source_type", "since", "until", "as_of"):
        kw = _gap_default_kwargs()
        kw[k] = "x"
        _log_recall_gap("scoped", [], n=10, **kw)
        assert not gap_path.exists(), f"gate {k} did not skip gap log"

    for boolk in ("include_history", "include_obsolete"):
        kw = _gap_default_kwargs()
        kw[boolk] = True
        _log_recall_gap("scoped", [], n=10, **kw)
        assert not gap_path.exists(), f"gate {boolk} did not skip gap log"


def test_gap_log_truncates_long_queries(monkeypatch, tmp_path):
    """Query is truncated to 500 chars in the log to avoid runaway disk."""
    import json as _json

    from routes.recall import _log_recall_gap

    gap_path = _read_gap_log(monkeypatch, tmp_path)
    long_q = "x" * 1000
    _log_recall_gap(long_q, [], n=10, **_gap_default_kwargs())
    record = _json.loads(gap_path.read_text().strip())
    assert len(record["query"]) == 500


def test_gap_log_swallows_io_failure(monkeypatch, tmp_path):
    """If the log file is unwritable, the helper must not raise."""
    from routes.recall import _log_recall_gap

    # Point BRAIN_DIR at a path where mkdir will fail (a file, not dir)
    bogus_file = tmp_path / "blocker"
    bogus_file.write_text("x")
    import routes.recall as recall_mod

    monkeypatch.setattr(recall_mod, "BRAIN_DIR", bogus_file)
    # Must NOT raise — exception is swallowed by the helper
    _log_recall_gap("q", [], n=10, **_gap_default_kwargs())


def test_inhibition_only_inspects_top_5(monkeypatch):
    """The competition slice is fused[:5] regardless of caller's n —
    ranks 6+ never become losers."""
    import sys as _sys
    import types

    from routes.recall import _log_retrieval_inhibition

    bg = _FakeBgPool()
    _stub_ri = types.ModuleType("retrieval_inhibition")
    _stub_ri.log_competition = lambda *a, **k: None
    _stub_su = types.ModuleType("brain_core.search_unified")
    _stub_su._search_bg_pool = bg
    monkeypatch.setitem(_sys.modules, "retrieval_inhibition", _stub_ri)
    monkeypatch.setitem(_sys.modules, "brain_core.search_unified", _stub_su)

    fused = [
        {"id": "atm_1", "collection": "semantic_memory"},
        {"id": "atm_2", "collection": "semantic_memory"},
        {"id": "atm_3", "collection": "semantic_memory"},
        {"id": "atm_4", "collection": "semantic_memory"},
        {"id": "atm_5", "collection": "semantic_memory"},
        {"id": "atm_6", "collection": "semantic_memory"},  # outside competition slice
        {"id": "atm_7", "collection": "semantic_memory"},  # outside competition slice
    ]
    _log_retrieval_inhibition(fused, "q")
    assert len(bg.calls) == 1
    _, args, _kw = bg.calls[0]
    winner, losers, _q = args
    assert winner == "atm_1"
    assert losers == ["atm_2", "atm_3", "atm_4", "atm_5"]


def test_metacognitive_surface_top_n_cutoff_respected(monkeypatch):
    """Only the first top_n rows are inspected for semantic_memory ids;
    rows past the cutoff are not enriched with confidence."""
    import atoms_store
    import confidence_calibration
    from routes import recall as recall_mod
    from routes.recall import _apply_metacognitive_surface_inplace

    conn = _FakeAtomsConn(rows=[{"chroma_id": "atm_1", "confidence": 0.9, "trust_score": 0.9}])
    monkeypatch.setattr(atoms_store, "_conn", lambda *a, **k: _FakeAtomsConnCtx(conn))
    monkeypatch.setattr(confidence_calibration, "apply_calibration", lambda x: x)
    monkeypatch.setattr(recall_mod, "get_vector_store", lambda: _FakeVectorStore(points=[]))

    fused = [
        {"id": "atm_1", "collection": "semantic_memory"},
        {"id": "atm_2", "collection": "semantic_memory"},  # outside top_n
    ]
    _apply_metacognitive_surface_inplace(fused, top_n=1)
    assert "confidence" in fused[0]
    assert "confidence" not in fused[1]
    # Only atm_1 was passed to the SQL placeholder list.
    assert conn.last_params == ["atm_1"]


# ── Server-side recall governance ──────────────────────────────────────


def test_recall_governance_promotes_specific_preference_over_weekly_summary():
    from routes.recall import _apply_recall_governance_inplace

    fused = [
        {
            "id": "weekly",
            "title": "W20 weekly brain summary",
            "path": "/canonical/weekly/W20-summary.md",
            "collection": "canonical",
            "type": "weekly-summary",
            "content": "General weekly brain summary with database deployment mentioned once.",
            "score": 100.0,
        },
        {
            "id": "decision",
            "title": "Database deployment workflow decision",
            "path": "/canonical/decisions/database-deployment-workflow.md",
            "collection": "semantic_memory",
            "metadata": {"category": "decision"},
            "content": "Chris prefers database deployment workflow via migrations, verification, and rollback checks.",
            "score": 80.0,
        },
    ]

    _apply_recall_governance_inplace("database deployment workflow recommendation", fused)

    assert fused[1]["score"] > fused[0]["score"]
    assert "specific_truth" in fused[1]["governance"]
    assert "generic_summary_penalty" in fused[0]["governance"]


def test_recall_governance_prefers_accepted_canonical_truth_for_image_generation():
    from routes.recall import _apply_recall_governance_inplace

    fused = [
        {
            "id": "generic-image",
            "title": "Image generation notes",
            "path": "/notes/image.md",
            "collection": "semantic_memory",
            "content": "Use generic API image generation when asked for images.",
            "score": 92.0,
        },
        {
            "id": "accepted-pref",
            "title": "Image generation provider preference",
            "path": "/canonical/preferences/image-generation-openai-codex-oauth.md",
            "collection": "canonical",
            "metadata": {"category": "preference", "review_state": "accepted"},
            "content": "For image generation, Chris expects GPT Images / OpenAI through Codex OAuth or subscription CLI, not separate paid API assumptions.",
            "score": 75.0,
        },
    ]

    _apply_recall_governance_inplace("이미지 생성 추천", fused)

    assert fused[1]["score"] > fused[0]["score"]
    assert "canonical_accepted" in fused[1]["governance"]
    assert "specific_truth" in fused[1]["governance"]


def test_recall_governance_openclaw_hermes_distinction_beats_setup_and_live_state_noise():
    from routes.recall import _apply_recall_governance_inplace

    fused = [
        {
            "id": "setup-docs",
            "title": "OpenClaw Multi-Agent Setup Documentation",
            "path": "/Users/chrischo/.openclaw/workspace-claude/AGENTS.md",
            "collection": "obsidian",
            "content": "OpenClaw setup docs from the old workspace mention Hermes runtime migration history.",
            "score": 235.0,
        },
        {
            "id": "active-goals",
            "title": "active_goals",
            "path": "/Users/chrischo/server/knowledge/canonical/live_state/active_goals.md",
            "collection": "canonical",
            "metadata": {"document_type": "canonical-note"},
            "content": "Active goals and focus: OpenClaw Hermes current runtime tasks and manual focus items.",
            "score": 225.0,
        },
        {
            "id": "raptor-live-snapshot",
            "title": "",
            "path": "raptor:L1:142:20260524",
            "collection": "canonical",
            "content": (
                "These notes center on Chris's OpenClaw/Brain operating model. "
                "Several notes are current-state snapshots, explicitly regenerated every 10 minutes "
                "by live_state_snapshot cron and not historical records."
            ),
            "score": 230.0,
        },
        {
            "id": "handoff-noise",
            "title": "hermes",
            "path": "hermes",
            "collection": "semantic_memory",
            "metadata": {"category": "fact"},
            "content": (
                "User: work kanban task t_be1032bb Assistant: Verdict: PARTIAL. "
                "Acceptance probes: OpenClaw/Hermes exact row top3, no live_state/setup noise. "
                "Focused tests passed, but dirty patch still needs review."
            ),
            "score": 245.0,
        },
        {
            "id": "eval-noise",
            "title": "hermes",
            "path": "hermes",
            "collection": "semantic_memory",
            "metadata": {"category": "fact"},
            "content": (
                "Generic regression spot check: OpenClaw vs Hermes current runtime historical distinction "
                "still has live-state and setup noise in top10; generic_recipe_knowledge_gap surfaced."
            ),
            "score": 250.0,
        },
        {
            "id": "distinction",
            "title": "OpenClaw vs Hermes current runtime historical distinction",
            "path": "/distilled/openclaw-hermes-distinction.md",
            "collection": "semantic_memory",
            "metadata": {"category": "decision", "review_state": "accepted"},
            "content": (
                "Distilled current distinction: Hermes Agent is the current runtime; "
                "OpenClaw is historical provenance and setup context."
            ),
            "score": 150.0,
        },
    ]

    _apply_recall_governance_inplace("OpenClaw vs Hermes current runtime historical distinction", fused)
    fused.sort(key=lambda r: r["score"], reverse=True)

    assert fused[0]["id"] == "distinction"
    assert "openclaw_hermes_distinction" in fused[0]["governance"]
    by_id = {row["id"]: row for row in fused}
    assert "openclaw_setup_noise_penalty" in by_id["setup-docs"].get("governance", [])
    assert "live_state_snapshot_penalty" in by_id["active-goals"].get("governance", [])
    assert "live_state_snapshot_penalty" in by_id["raptor-live-snapshot"].get("governance", [])
    assert "openclaw_distinction_handoff_penalty" in by_id["handoff-noise"].get("governance", [])
    assert "openclaw_distinction_handoff_penalty" in by_id["eval-noise"].get("governance", [])
    assert by_id["handoff-noise"]["score"] < by_id["distinction"]["score"]
    assert by_id["eval-noise"]["score"] < by_id["distinction"]["score"]


def test_recall_governance_codex_hermes_tui_preference_beats_old_claude_restriction():
    from routes.recall import _apply_recall_governance_inplace

    fused = [
        {
            "id": "old-claude-restriction",
            "title": "Claude Code usage restriction",
            "path": "/notes/claude-code-restrictions.md",
            "collection": "semantic_memory",
            "content": "Old Claude Code plan-mode restrictions and usage caveats for coding tasks.",
            "score": 220.0,
        },
        {
            "id": "codex-skill-sync-noise",
            "title": "Codex/Claude Code skill sync",
            "path": "/Users/chrischo/.hermes/skills/autonomous-ai-agents/codex/SKILL.md",
            "collection": "semantic_memory",
            "content": (
                "Codex and Claude Code skill sync note: add headless codex exec and tmux TUI snippets "
                "to the autonomous-ai-agents skills."
            ),
            "score": 254.0,
        },
        {
            "id": "codex-current-pref",
            "title": "Codex Hermes interactive tmux TUI preference",
            "path": "/canonical/preferences/codex-hermes-tmux-tui.md",
            "collection": "canonical",
            "metadata": {"category": "preference", "review_state": "accepted"},
            "content": (
                "Chris prefers using Codex through Hermes as an interactive terminal-like tmux TUI "
                "when quality or steering matters; headless codex exec is only for bounded automation."
            ),
            "score": 140.0,
        },
    ]

    _apply_recall_governance_inplace("복잡한 코딩 작업은 코덱스를 어떻게 쓰는 게 좋아?", fused)
    fused.sort(key=lambda r: r["score"], reverse=True)

    assert fused[0]["id"] == "codex-current-pref"
    assert "codex_hermes_tui_preference" in fused[0]["governance"]
    assert "old_claude_code_restriction_penalty" in next(
        r for r in fused if r["id"] == "old-claude-restriction"
    ).get("governance", [])
    assert "codex_skill_sync_noise_penalty" in next(
        r for r in fused if r["id"] == "codex-skill-sync-noise"
    ).get("governance", [])


def test_korean_intent_expansion_adds_provider_independent_terms():
    from routes.recall import _augment_query_for_recall

    expanded = _augment_query_for_recall("음악 음성 과금 유료 로컬 추천")

    assert "music" in expanded
    assert "tts" in expanded
    assert "billing" in expanded
    assert "paid api" in expanded
    assert "local generation" in expanded
    assert "recommendation" in expanded


def test_korean_codex_recommendation_expansion_adds_current_workflow_terms():
    from routes.recall import _augment_query_for_recall

    expanded = _augment_query_for_recall("복잡한 코딩 작업은 코덱스를 어떻게 쓰는 게 좋아?")

    assert "codex" in expanded.lower()
    assert "hermes" in expanded.lower()
    assert "tmux" in expanded.lower()
    assert "tui" in expanded.lower()
    assert "headless codex exec" in expanded.lower()


def test_korean_calendar_reminder_class_expansion_adds_schedule_terms():
    from routes.recall import _augment_query_for_recall

    expanded = _augment_query_for_recall("수업 캘린더 리마인더 추천")

    assert "class schedule" in expanded
    assert "calendar" in expanded
    assert "macos-calendar" in expanded
    assert "apple-reminders" in expanded
    assert "primary tooling choices" in expanded


def test_budget_local_cloud_intent_expansion_is_class_based_not_smoke_literal():
    from routes.recall import _KOREAN_INTENT_EXPANSIONS, _augment_query_for_recall

    expanded = _augment_query_for_recall("agent workflow without another paid provider, local or hosted?")

    assert "avoid new paid api" in expanded
    assert "existing subscription" in expanded
    assert "local first" in expanded
    assert "cloud only when already available" in expanded
    expanded_lower = expanded.lower()
    assert "no new paid" not in expanded_lower
    assert "local vs cloud" not in expanded_lower
    assert "local-vs-cloud" not in expanded_lower
    assert "automation workflow" not in expanded_lower
    expansion_text = " ".join(term for terms in _KOREAN_INTENT_EXPANSIONS.values() for term in terms).lower()
    assert "no new paid" not in _KOREAN_INTENT_EXPANSIONS
    assert "no new paid" not in expansion_text
    assert "local vs cloud" not in expansion_text
    assert "local-vs-cloud" not in expansion_text
    assert "automation workflow" not in expansion_text


def test_recall_governance_budget_local_cloud_preference_beats_generic_summary():
    from routes.recall import _apply_recall_governance_inplace

    fused = [
        {
            "id": "generic",
            "title": "W20 weekly brain summary",
            "path": "/canonical/weekly/W20-summary.md",
            "collection": "canonical",
            "type": "weekly-summary",
            "content": "A broad weekly summary mentioning automation, workflow, paid APIs, local tools, and cloud options.",
            "score": 180.0,
        },
        {
            "id": "constraint",
            "title": "Automation workflow cost and hosting preference",
            "path": "/canonical/preferences/automation-workflow-cost-hosting.md",
            "collection": "canonical",
            "metadata": {"category": "preference", "review_state": "accepted"},
            "content": "Chris prefers automation workflows that avoid separate paid APIs and use local tools unless an existing subscription or already-available cloud workflow fits.",
            "score": 70.0,
        },
    ]

    _apply_recall_governance_inplace(
        "Which agent pipeline should avoid another paid provider and run locally or hosted?", fused
    )

    assert fused[1]["score"] > fused[0]["score"]
    assert "budget_local_cloud_constraint" in fused[1]["governance"]
    assert "generic_summary_penalty" in fused[0]["governance"]


def test_recall_governance_budget_local_cloud_boost_requires_domain_overlap():
    from routes.recall import _apply_recall_governance_inplace

    fused = [
        {
            "id": "workflow",
            "title": "Workflow preferences",
            "path": "/canonical/preferences/workflow.md",
            "collection": "canonical",
            "metadata": {"category": "preference", "review_state": "accepted"},
            "content": "Chris prefers agent workflows to avoid another paid provider and choose local tools or already available hosted workflows.",
            "score": 70.0,
        },
        {
            "id": "music",
            "title": "Music and TTS recommendation billing constraints",
            "path": "/canonical/preferences/music-tts-no-local-no-paid-api.md",
            "collection": "canonical",
            "metadata": {"category": "preference", "review_state": "accepted"},
            "content": "Recommendation for music and TTS: avoid local generation and avoid separate paid SaaS/API billing unless Chris explicitly asks.",
            "score": 90.0,
        },
    ]

    _apply_recall_governance_inplace(
        "Which agent pipeline should avoid another paid provider and run locally or hosted?", fused
    )

    assert fused[0]["score"] > fused[1]["score"]
    assert "budget_local_cloud_constraint" in fused[0]["governance"]
    assert "budget_local_cloud_constraint" not in fused[1].get("governance", [])


def test_recall_governance_budget_local_cloud_broad_recommendation_is_not_domain_overlap():
    from routes.recall import _apply_recall_governance_inplace

    fused = [
        {
            "id": "workflow",
            "title": "Agent workflow recommendation",
            "path": "/canonical/preferences/agent-workflow-cost-hosting.md",
            "collection": "canonical",
            "metadata": {"category": "preference", "review_state": "accepted"},
            "content": "Chris prefers agent workflows to avoid another paid provider and choose local tools or already available hosted workflows.",
            "score": 70.0,
        },
        {
            "id": "music",
            "title": "Music and TTS recommendation billing constraints",
            "path": "/canonical/preferences/music-tts-no-local-no-paid-api.md",
            "collection": "canonical",
            "metadata": {"category": "preference", "review_state": "accepted"},
            "content": "Recommendation for music and TTS: avoid local generation and avoid separate paid SaaS/API billing unless Chris explicitly asks.",
            "score": 100.0,
        },
    ]

    _apply_recall_governance_inplace(
        "Which agent recommendation should avoid another paid provider and run locally or hosted?",
        fused,
    )

    assert fused[0]["score"] > fused[1]["score"]
    assert "budget_local_cloud_constraint" in fused[0]["governance"]
    assert "budget_local_cloud_constraint" not in fused[1].get("governance", [])


def test_korean_status_query_is_classified_as_live_state():
    from routes.recall import _is_live_state_query

    assert _is_live_state_query("칸반 태스크 진행상황 시작했어 완료?") is True
    assert _is_live_state_query("이미지 생성 추천") is False
    assert _is_live_state_query("작업 방식 추천") is False


def test_korean_colloquial_running_query_is_classified_as_live_state():
    from routes.recall import _is_live_state_query

    assert _is_live_state_query("지금 뭐 돌아가고 있어?") is True


def test_korean_recommendation_queries_with_running_phrase_are_not_live_state():
    from routes.recall import _is_live_state_query

    assert _is_live_state_query("요즘 잘 돌아가는 로컬 모델 추천") is False
    assert _is_live_state_query("현재 잘 돌아가는 오픈소스 TTS 추천") is False
    assert _is_live_state_query("현재 어떤 로컬 모델이 잘 돌아가는지 추천해줘") is False


def test_live_state_query_requires_explicit_status_intent():
    from routes.recall import _is_live_state_query

    assert _is_live_state_query("current status of kanban task t_12345678") is True
    assert _is_live_state_query("progress update for recall governance") is True
    assert _is_live_state_query("what is running right now") is True
    assert _is_live_state_query("current Brain mission progress") is True
    assert _is_live_state_query("current alpha project status") is True
    assert _is_live_state_query("complete guide to recall governance") is False
    assert _is_live_state_query("completed migration decision") is False
    assert _is_live_state_query("started workflow preference") is False
    assert _is_live_state_query("running local inference decision") is False


def test_live_state_query_does_not_suppress_durable_preference_status_queries():
    from routes.recall import _is_live_state_query

    assert _is_live_state_query("durable current preference for kanban status recall governance") is False
    assert _is_live_state_query("Chris current status-control preference from memory") is False
    assert _is_live_state_query("canonical decision about task status governance") is False


def test_recall_v2_durable_completion_query_does_not_short_circuit(monkeypatch):
    from routes import recall as recall_route
    from starlette.requests import Request

    recall_route._recall_cache.clear()
    calls: list[dict] = []

    def fake_search_all(query, n, **kwargs):
        calls.append({"query": query, "n": n, **kwargs})
        return {"results": [], "total_candidates": 0, "source_timing": {}}

    monkeypatch.setattr(recall_route.search_unified, "search_all", fake_search_all)

    request = Request(
        {"type": "http", "method": "GET", "path": "/recall/v2", "headers": [], "query_string": b""}
    )
    response = recall_route.recall_v2(
        request,
        q="complete guide to recall governance",
        n=3,
        rerank=False,
        decay=False,
    )

    assert calls, "durable completion query should search instead of short-circuiting as live state"
    assert response.timing.get("live_state_query") is None
    assert response.meta_note != "Live-state/status query — use live tools instead of stale memory recall."


def test_recall_v2_durable_status_preference_query_searches_memory(monkeypatch):
    from routes import recall as recall_route
    from starlette.requests import Request

    recall_route._recall_cache.clear()
    calls: list[dict] = []

    def fake_search_all(query, n, **kwargs):
        calls.append({"query": query, "n": n, **kwargs})
        return {"results": [], "total_candidates": 0, "source_timing": {}}

    monkeypatch.setattr(recall_route.search_unified, "search_all", fake_search_all)

    request = Request(
        {"type": "http", "method": "GET", "path": "/recall/v2", "headers": [], "query_string": b""}
    )
    response = recall_route.recall_v2(
        request,
        q="durable current preference for kanban status recall governance",
        n=3,
        rerank=False,
        decay=False,
    )

    assert calls, "durable status preference query should search instead of short-circuiting as live state"
    assert response.timing.get("live_state_query") is None
    assert response.meta_note != "Live-state/status query — use live tools instead of stale memory recall."


def test_recall_v2_status_query_short_circuits_before_search(monkeypatch):
    from routes import recall as recall_route
    from starlette.requests import Request

    recall_route._recall_cache.clear()
    calls: list[str] = []

    def fake_search_all(*args, **kwargs):
        calls.append("called")
        return {"results": [], "total_candidates": 0, "source_timing": {}}

    monkeypatch.setattr(recall_route.search_unified, "search_all", fake_search_all)

    request = Request(
        {"type": "http", "method": "GET", "path": "/recall/v2", "headers": [], "query_string": b""}
    )
    response = recall_route.recall_v2(
        request,
        q="current status of kanban task t_12345678",
        n=3,
        rerank=False,
        decay=False,
    )

    assert calls == []
    assert response.results == []
    assert response.timing["live_state_query"] is True
    assert response.meta_note == "Live-state/status query — use live tools instead of stale memory recall."


def test_recall_v2_korean_colloquial_running_query_short_circuits_before_search(monkeypatch):
    from routes import recall as recall_route
    from starlette.requests import Request

    recall_route._recall_cache.clear()
    calls: list[str] = []

    def fake_search_all(*args, **kwargs):
        calls.append("called")
        return {"results": [], "total_candidates": 0, "source_timing": {}}

    monkeypatch.setattr(recall_route.search_unified, "search_all", fake_search_all)

    request = Request(
        {"type": "http", "method": "GET", "path": "/recall/v2", "headers": [], "query_string": b""}
    )
    response = recall_route.recall_v2(
        request,
        q="지금 뭐 돌아가고 있어?",
        n=3,
        rerank=False,
        decay=False,
    )

    assert calls == []
    assert response.results == []
    assert response.timing["live_state_query"] is True
    assert response.meta_note == "Live-state/status query — use live tools instead of stale memory recall."


def test_recall_v2_calendar_tooling_query_searches_preference_variant(monkeypatch):
    from routes import recall as recall_route
    from starlette.requests import Request

    recall_route._recall_cache.clear()
    seen_queries: list[str] = []
    seen_limits: list[int] = []

    def fake_search_all(query, n, **kwargs):
        seen_queries.append(query)
        seen_limits.append(n)
        return {
            "results": [
                {
                    "id": query,
                    "title": "placeholder",
                    "path": "/tmp/placeholder.md",
                    "collection": "knowledge",
                    "content": query,
                    "score": 1.0,
                }
            ],
            "total_candidates": 1,
            "source_timing": {},
        }

    monkeypatch.setattr(recall_route.search_unified, "search_all", fake_search_all)

    request = Request(
        {"type": "http", "method": "GET", "path": "/recall/v2", "headers": [], "query_string": b""}
    )
    recall_route.recall_v2(
        request,
        q="캘린더 리마인더 추천",
        n=3,
        rerank=False,
        decay=False,
    )

    assert any("Apple Calendar Reminders 도구 흐름 선호" in query for query in seen_queries)
    assert min(seen_limits) >= 80


def test_recall_v2_simple_apple_calendar_reminders_query_searches_preference_variant(monkeypatch):
    from routes import recall as recall_route
    from starlette.requests import Request

    recall_route._recall_cache.clear()
    seen_queries: list[str] = []
    seen_limits: list[int] = []

    def fake_search_all(query, n, **kwargs):
        seen_queries.append(query)
        seen_limits.append(n)
        return {
            "results": [
                {
                    "id": query,
                    "title": "placeholder",
                    "path": "/tmp/placeholder.md",
                    "collection": "knowledge",
                    "content": query,
                    "score": 1.0,
                }
            ],
            "total_candidates": 1,
            "source_timing": {},
        }

    monkeypatch.setattr(recall_route.search_unified, "search_all", fake_search_all)

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/recall/v2",
            "headers": [(b"x-agent", b"sage")],
            "query_string": b"",
        }
    )
    recall_route.recall_v2(
        request,
        q="Apple Calendar/Reminders",
        n=3,
        rerank=False,
        decay=False,
    )

    assert any("Apple Calendar Reminders 도구 흐름 선호" in query for query in seen_queries)
    assert min(seen_limits) >= 80


def test_recall_v2_codex_workflow_query_searches_current_preference_variant(monkeypatch):
    import threading

    from routes import recall as recall_route
    from starlette.requests import Request

    recall_route._recall_cache.clear()
    seen_queries: list[str] = []
    seen_limits: list[int] = []
    seen_threads: list[str] = []

    def fake_search_all(query, n, **kwargs):
        seen_queries.append(query)
        seen_limits.append(n)
        seen_threads.append(threading.current_thread().name)
        return {
            "results": [
                {
                    "id": query,
                    "title": "placeholder",
                    "path": "/tmp/placeholder.md",
                    "collection": "knowledge",
                    "content": query,
                    "score": 1.0,
                }
            ],
            "total_candidates": 1,
            "source_timing": {},
        }

    monkeypatch.setattr(recall_route.search_unified, "search_all", fake_search_all)

    request = Request(
        {"type": "http", "method": "GET", "path": "/recall/v2", "headers": [], "query_string": b""}
    )
    recall_route.recall_v2(
        request,
        q="복잡한 코딩 작업은 코덱스를 어떻게 쓰는 게 좋아?",
        n=3,
        rerank=False,
        decay=False,
        agent=None,
        source_type=None,
        include_history=False,
        include_obsolete=False,
        as_of=None,
        canonical_first=False,
        exclude_already_used=False,
    )

    assert any("Codex Hermes interactive tmux TUI preference" in query for query in seen_queries)
    assert min(seen_limits) >= 80
    assert seen_threads
    assert set(seen_threads) == {threading.current_thread().name}


def test_recall_v2_openclaw_hermes_query_searches_distinction_variant(monkeypatch):
    from routes import recall as recall_route
    from starlette.requests import Request

    recall_route._recall_cache.clear()
    seen_queries: list[str] = []
    seen_limits: list[int] = []

    def fake_search_all(query, n, **kwargs):
        seen_queries.append(query)
        seen_limits.append(n)
        return {
            "results": [
                {
                    "id": query,
                    "title": "placeholder",
                    "path": "/tmp/placeholder.md",
                    "collection": "knowledge",
                    "content": query,
                    "score": 1.0,
                }
            ],
            "total_candidates": 1,
            "source_timing": {},
        }

    monkeypatch.setattr(recall_route.search_unified, "search_all", fake_search_all)

    request = Request(
        {"type": "http", "method": "GET", "path": "/recall/v2", "headers": [], "query_string": b""}
    )
    recall_route.recall_v2(
        request,
        q="OpenClaw vs Hermes current runtime historical distinction",
        n=3,
        rerank=False,
        decay=False,
    )

    assert any(query == "OpenClaw Hermes current runtime historical distinction" for query in seen_queries)
    assert min(seen_limits) >= 80


def test_recall_governance_music_tts_billing_constraints_win():
    from routes.recall import _apply_recall_governance_inplace

    fused = [
        {
            "id": "saas",
            "title": "Music API recommendation",
            "path": "/notes/music-api.md",
            "collection": "semantic_memory",
            "content": "Recommend a separate paid SaaS API for music and TTS generation.",
            "score": 92.0,
        },
        {
            "id": "constraint",
            "title": "Music and TTS billing constraints",
            "path": "/canonical/preferences/music-tts-no-local-no-paid-api.md",
            "collection": "canonical",
            "metadata": {"category": "preference", "review_state": "accepted"},
            "content": "For music and TTS, avoid local generation and avoid separate paid SaaS/API billing unless Chris explicitly asks.",
            "score": 74.0,
        },
    ]

    _apply_recall_governance_inplace("음악 음성 과금 유료 로컬 추천", fused)

    assert fused[1]["score"] > fused[0]["score"]


def test_korean_music_tts_paid_api_avoidance_query_gets_budget_expansion():
    from routes.recall import _augment_query_for_recall

    expanded = _augment_query_for_recall("배경음악이나 TTS 만들 때 로컬 모델 설치나 새 유료 API는 피해야 해?")

    assert "background music" in expanded
    assert "tts" in expanded.lower()
    assert "avoid" in expanded
    assert "new" in expanded
    assert "avoid new paid api" in expanded
    assert "no separate paid api" in expanded
    assert "cost conscious existing subscriptions integrations no local model hosting music TTS" in expanded


def test_recall_governance_music_tts_exact_class_query_penalizes_api_key_noise():
    from routes.recall import _apply_recall_governance_inplace

    fused = [
        {
            "id": "token-noise",
            "title": "Cloudflare API token troubleshooting",
            "path": "/learnings/errors.md",
            "collection": "experience",
            "content": "Invalid API key. Check token length, bearer auth, hex token format, and external API key setup.",
            "score": 146.0,
        },
        {
            "id": "preference",
            "title": "Music and TTS billing constraints",
            "path": "/semantic/hermes",
            "collection": "semantic_memory",
            "metadata": {"category": "preference"},
            "content": "Chris corrected that music and TTS should avoid local model generation and avoid separate paid API or external paid service unless approved.",
            "score": 90.0,
        },
    ]

    _apply_recall_governance_inplace(
        "배경음악이나 TTS 만들 때 로컬 모델 설치나 새 유료 API는 피해야 해?", fused
    )

    assert fused[1]["score"] > fused[0]["score"]
    assert "budget_local_cloud_constraint" in fused[1]["governance"]
    assert "generic_api_troubleshooting_penalty" in fused[0]["governance"]


def test_recall_governance_apple_calendar_reminders_preference_wins():
    from routes.recall import _apply_recall_governance_inplace

    fused = [
        {
            "id": "google",
            "title": "Google Calendar default",
            "path": "/notes/google-calendar.md",
            "collection": "semantic_memory",
            "content": "Use Google Calendar and Google Tasks as the default scheduling tools.",
            "score": 90.0,
        },
        {
            "id": "apple",
            "title": "Apple Calendar and Reminders preference",
            "path": "/canonical/preferences/apple-calendar-reminders.md",
            "collection": "canonical",
            "metadata": {"category": "preference", "review_state": "accepted"},
            "content": "Chris uses Apple Calendar and Apple Reminders for calendar events and reminders.",
            "score": 72.0,
        },
    ]

    _apply_recall_governance_inplace("캘린더 리마인더 추천", fused)

    assert fused[1]["score"] > fused[0]["score"]


def test_recall_governance_simple_apple_calendar_reminders_promotes_distilled_analysis_over_obsidian_noise():
    from routes.recall import _apply_recall_governance_inplace

    for query in ("Apple Calendar/Reminders", "Apple Calendar Reminders"):
        fused = [
            {
                "id": "obsidian-brain-architecture",
                "title": "semantic",
                "path": (
                    "/Users/chrischo/Library/Mobile Documents/iCloud~md~obsidian/Documents/"
                    "Obsidian-vault/Chrischodev/brain-system-architecture.md"
                ),
                "collection": "obsidian",
                "content": "Brain system architecture notes mention Apple Calendar and Reminders bridge sources.",
                "score": 164.95,
            },
            {
                "id": "apple-analysis",
                "title": "Analysis: 크리스가 Apple Calendar/Reminders 작업을 부탁하면 어떤 도구/흐름을 선호하나?",
                "path": "distilled/decisions/brain_analysis_73739b05d41b.md",
                "collection": "distilled",
                "metadata": {"subtype": "brain-analysis"},
                "content": (
                    "Chris prefers Brain-backed recall/bridging for Apple Reminders tasks. "
                    "For Apple Calendar access, use bounded/fallback access. "
                    "For Calendar/Reminders specifically, Reminders via Brain source records and "
                    "Calendar via bounded/fallback access are the key preference."
                ),
                "score": 145.11,
            },
        ]

        _apply_recall_governance_inplace(query, fused)
        fused.sort(key=lambda row: row["score"], reverse=True)

        assert fused[0]["id"] == "apple-analysis"
        assert "primary_tooling_choice" in fused[0]["governance"]
        assert "calendar_tooling_offtopic_penalty" in fused[1]["governance"]


def test_recall_governance_tooling_choice_beats_completed_reminder_for_calendar_tool_query():
    from routes.recall import _apply_recall_governance_inplace

    fused = [
        {
            "id": "completed-reminder",
            "title": "reminders://Reminders/36",
            "path": "reminders://Reminders/36",
            "collection": "personal",
            "content": "Reminder: 저녁 약속 List: Reminders Status: completed Due: 2026-03-13",
            "score": 105.0,
        },
        {
            "id": "primary-tooling",
            "title": "Primary Tooling Choices",
            "path": "/knowledge/tooling.md",
            "collection": "knowledge",
            "content": "Reminders: `apple-reminders` primary. Calendar: `macos-calendar` primary local calendar, `google-workspace-mcp` for Google side.",
            "score": 70.0,
        },
    ]

    _apply_recall_governance_inplace("캘린더 리마인더 수업 일정은 어떤 도구로 관리해야 해?", fused)

    assert fused[1]["score"] > fused[0]["score"]
    assert "primary_tooling_choice" in fused[1]["governance"]
    assert "personal_instance_penalty" in fused[0]["governance"]


def test_recall_governance_apple_tooling_choice_beats_business_automation_noise():
    from routes.recall import _apply_recall_governance_inplace

    fused = [
        {
            "id": "business-automation",
            "title": "Business Plan - Automation Platform for Small Businesses",
            "path": "/obsidian/business/automation-platform.md",
            "collection": "obsidian",
            "content": "Business automation plan mentioning calendar automation and reminders in a generic small-business workflow.",
            "score": 180.0,
        },
        {
            "id": "apple-primary-tooling",
            "title": "Primary Tooling Choices",
            "path": "/knowledge/tooling.md",
            "collection": "knowledge",
            "content": "Reminders: `apple-reminders` primary. Calendar: `macos-calendar` primary local calendar, `google-workspace-mcp` for Google side.",
            "score": 70.0,
        },
    ]

    _apply_recall_governance_inplace("Apple Calendar Reminders Chris preferred tools automation", fused)

    assert fused[1]["score"] > fused[0]["score"]
    assert "primary_tooling_choice" in fused[1]["governance"]
    assert "calendar_tooling_offtopic_penalty" in fused[0]["governance"]


def test_recall_governance_primary_tooling_query_promotes_calendar_analysis_over_brain_reflect_noise():
    from routes.recall import _apply_recall_governance_inplace

    fused = [
        {
            "id": "brain-reflect",
            "title": "brain-reflect:nightly",
            "path": "brain-reflect:nightly",
            "collection": "knowledge",
            "metadata": {"document_type": "pattern", "source_path": "brain-reflect:nightly"},
            "content": "Consistent push toward using Brain as primary source of truth and MCP-first tooling for reliable answers.",
            "score": 231.6,
        },
        {
            "id": "apple-analysis",
            "title": "Analysis: 크리스가 Apple Calendar/Reminders 작업을 부탁하면 어떤 도구/흐름을 선호하나?",
            "path": "distilled/decisions/brain_analysis_73739b05d41b.md",
            "collection": "distilled",
            "metadata": {
                "id": "dist_brain_analysis_73739b05d41b",
                "source_path": "distilled/decisions/brain_analysis_73739b05d41b.md",
                "document_type": "distilled",
            },
            "content": (
                "Chris prefers Brain-backed recall/bridging for Apple Reminders tasks. "
                "For Apple Calendar access, use bounded/fallback access. "
                "For Calendar/Reminders specifically, Reminders via Brain source records and "
                "Calendar via bounded/fallback access are the key preference."
            ),
            "score": 128.4,
        },
    ]

    _apply_recall_governance_inplace("Primary Tooling Choices Apple Calendar Reminders", fused)
    fused.sort(key=lambda row: row["score"], reverse=True)

    assert fused[0]["id"] == "apple-analysis"
    assert "primary_tooling_choice" in fused[0]["governance"]
    assert "calendar_tooling_offtopic_penalty" in fused[1]["governance"]


def test_recall_governance_penalizes_openclaw_skill_inventory_for_calendar_tooling_query():
    from routes.recall import _apply_recall_governance_inplace

    fused = [
        {
            "id": "openclaw-tools",
            "title": "Primary Tooling Choices",
            "path": "/Users/chrischo/.openclaw/workspace-jenna/TOOLS.md",
            "collection": "knowledge",
            "content": "Primary Tooling Choices\n- `apple-reminders`\n- `macos-calendar`\n- `google-workspace-mcp`",
            "score": 120.0,
        },
        {
            "id": "apple-analysis",
            "title": "Analysis: 크리스가 Apple Calendar/Reminders 작업을 부탁하면 어떤 도구/흐름을 선호하나?",
            "path": "distilled/decisions/brain_analysis_73739b05d41b.md",
            "collection": "distilled",
            "metadata": {"subtype": "brain-analysis"},
            "content": "Chris prefers Apple Calendar for calendar events and Apple Reminders for reminders; use local macOS automation first.",
            "score": 55.0,
        },
    ]

    _apply_recall_governance_inplace("캘린더 리마인더 추천", fused)

    assert fused[1]["score"] > fused[0]["score"]
    assert "primary_tooling_choice" not in fused[0].get("governance", [])
    assert "calendar_tooling_offtopic_penalty" in fused[0]["governance"]
    assert "primary_tooling_choice" in fused[1]["governance"]


def test_recall_governance_high_score_openclaw_tools_inventory_loses_to_distilled_calendar_analysis():
    from routes.recall import _apply_recall_governance_inplace

    fused = [
        {
            "id": "openclaw-tools",
            "title": "Primary Tooling Choices",
            "path": "/Users/chrischo/.openclaw/workspace-jenna/TOOLS.md",
            "collection": "knowledge",
            "content": "Primary Tooling Choices\n- `apple-reminders`\n- `macos-calendar`\n- `google-workspace-mcp`",
            # Live failure pre-governance shape: after the existing +26 title/topical
            # and -85 off-topic adjustments, this remained rank1 at ~306.
            "score": 365.14,
        },
        {
            "id": "apple-analysis",
            "title": "Analysis: 크리스가 Apple Calendar/Reminders 작업을 부탁하면 어떤 도구/흐름을 선호하나?",
            "path": "distilled/decisions/brain_analysis_73739b05d41b.md",
            "collection": "distilled",
            "metadata": {"subtype": "brain-analysis"},
            "content": (
                "Chris prefers Brain-backed recall/bridging for Apple Reminders tasks. "
                "For Apple Calendar access, use bounded/fallback access. "
                "For Calendar/Reminders specifically, Reminders via Brain source records and "
                "Calendar via bounded/fallback access are the key preference."
            ),
            # Live failure pre-governance shape: +116 governance lifted this only to
            # ~237, still below the OpenClaw inventory row.
            "score": 121.46,
        },
    ]

    _apply_recall_governance_inplace("Primary Tooling Choices Apple Calendar Reminders", fused)
    fused.sort(key=lambda row: row["score"], reverse=True)

    assert fused[0]["id"] == "apple-analysis"
    assert "primary_tooling_choice" not in fused[1].get("governance", [])
    assert "openclaw_calendar_inventory_penalty" in fused[1]["governance"]
    assert "calendar_tooling_offtopic_penalty" in fused[1]["governance"]
    assert "primary_tooling_choice" in fused[0]["governance"]


def test_recall_governance_penalizes_openclaw_agents_inventory_for_korean_class_schedule_query():
    from routes.recall import _apply_recall_governance_inplace

    fused = [
        {
            "id": "openclaw-agents",
            "title": "Google Workspace",
            "path": "/Users/chrischo/.openclaw/workspace-jenna/AGENTS.md",
            "collection": "knowledge",
            "content": "Skill inventory lists google-workspace, apple-reminders, macos-calendar, and other available tools.",
            "score": 110.0,
        },
        {
            "id": "apple-analysis",
            "title": "Analysis: 크리스가 Apple Calendar/Reminders 작업을 부탁하면 어떤 도구/흐름을 선호하나?",
            "path": "distilled/decisions/brain_analysis_73739b05d41b.md",
            "collection": "distilled",
            "metadata": {"subtype": "brain-analysis"},
            "content": "For class schedules, use Apple Calendar for events and Apple Reminders for reminder tasks.",
            "score": 40.0,
        },
    ]

    _apply_recall_governance_inplace("캘린더 리마인더 수업 일정은 어떤 도구로 관리해야 해?", fused)

    assert fused[1]["score"] > fused[0]["score"]
    assert "primary_tooling_choice" not in fused[0].get("governance", [])
    assert "calendar_tooling_offtopic_penalty" in fused[0]["governance"]
    assert "primary_tooling_choice" in fused[1]["governance"]


def test_retrieval_quality_filter_removes_generic_summary_rows_when_summary_excluded():
    from routes.recall import _apply_retrieval_quality_filter

    fused = [
        {
            "id": "generic-summary-1",
            "title": "Summary",
            "path": "/Users/chrischo/server/knowledge/distilled/infra/dist_weekly_summary.md",
            "collection": "distilled",
            "content": "A generic weekly summary mentioning broad tool recommendation preferences.",
            "score": 500.0,
        },
        {
            "id": "candidate-tool",
            "title": "tool_candidate",
            "path": "/canonical/tools/tool_candidate.md",
            "collection": "canonical",
            "metadata": {"category": "decision", "review_state": "accepted"},
            "content": "Tool: candidate. Chris prefers direct actionable tool recommendations, not generic weekly summaries.",
            "score": 100.0,
        },
        {
            "id": "generic-summary-2",
            "title": "Chris Cho — current state (regenerated weekly)",
            "path": "canonical/chris/_state.md",
            "collection": "canonical",
            "content": "Weekly regenerated state summary.",
            "score": 90.0,
        },
    ]

    filtered = _apply_retrieval_quality_filter("broad tool recommendation generic weekly summary 말고", fused)

    assert [result["id"] for result in filtered] == ["candidate-tool"]
    assert all(result["title"] != "Summary" for result in filtered[:3])


def test_retrieval_quality_filter_keeps_generic_summary_rows_for_positive_summary_intent():
    from routes.recall import _apply_retrieval_quality_filter

    fused = [
        {
            "id": "generic-summary",
            "title": "Summary",
            "path": "/Users/chrischo/server/knowledge/distilled/infra/dist_weekly_summary.md",
            "collection": "distilled",
            "content": "A weekly summary.",
            "score": 100.0,
        }
    ]

    filtered = _apply_retrieval_quality_filter("weekly summary recap", fused)

    assert [result["id"] for result in filtered] == ["generic-summary"]


def test_retrieval_quality_filter_removes_personal_noise_for_generic_recipe_query():
    from routes.recall import _apply_retrieval_quality_filter

    fused = [
        {
            "id": "agents-noise",
            "title": "AGENTS Make It Yours",
            "path": "/Users/chrischo/server/brain/AGENTS.md",
            "collection": "canonical",
            "content": "Make It Yours: agent workflow and codebase conventions.",
            "score": 250.0,
        },
        {
            "id": "identity-noise",
            "title": "Chris identity",
            "path": "/Users/chrischo/server/knowledge/canonical/chris/_identity.md",
            "collection": "canonical",
            "content": "Chris profile and preferences, unrelated to cooking.",
            "score": 225.0,
        },
        {
            "id": "recipe-note",
            "title": "Tomato pasta sauce recipe",
            "path": "/recipes/tomato-pasta.md",
            "collection": "knowledge",
            "content": "Tomato pasta sauce recipe steps: simmer tomatoes, garlic, olive oil, and basil.",
            "score": 90.0,
        },
    ]

    filtered = _apply_retrieval_quality_filter("how do I make tomato pasta sauce recipe steps", fused)

    assert [result["id"] for result in filtered] == ["recipe-note"]


def test_retrieval_quality_filter_removes_openclaw_hermes_acceptance_handoff_noise():
    from routes.recall import _apply_retrieval_quality_filter

    fused = [
        {
            "id": "distinction",
            "title": "OpenClaw vs Hermes current runtime historical distinction",
            "path": "mcp",
            "collection": "semantic_memory",
            "content": (
                "Chris is currently interacting with Jenna running on Hermes Agent; "
                "when comparing Hermes Agent vs OpenClaw, distinguish the historical "
                "platform decision from the current runtime context."
            ),
            "score": 150.0,
        },
        {
            "id": "handoff-noise",
            "title": "hermes",
            "path": "hermes",
            "collection": "semantic_memory",
            "content": (
                "Generic regression spot check: OpenClaw vs Hermes current runtime historical distinction "
                "still has live-state and setup noise in top10; generic_recipe_knowledge_gap surfaced."
            ),
            "score": 96.0,
        },
        {
            "id": "setup-noise",
            "title": "OpenClaw setup guide",
            "path": "/notes/openclaw-setup.md",
            "collection": "obsidian",
            "content": "Sub-Agent Configuration and Active Hours for Heartbeat in the old OpenClaw setup.",
            "score": 95.0,
        },
        {
            "id": "live-state-noise",
            "title": "active_goals",
            "path": "/Users/chrischo/server/knowledge/canonical/live_state/active_goals.md",
            "collection": "canonical",
            "content": "Active goals and focus: OpenClaw Hermes current runtime work queue.",
            "score": 94.0,
        },
    ]

    filtered = _apply_retrieval_quality_filter(
        "OpenClaw vs Hermes current runtime historical distinction",
        fused,
    )

    assert [result["id"] for result in filtered] == ["distinction"]


def test_recall_governance_openclaw_historical_loses_to_current_hermes_runtime():
    from routes.recall import _apply_recall_governance_inplace

    fused = [
        {
            "id": "openclaw",
            "title": "OpenClaw historical runtime",
            "path": "/archived/openclaw/runtime.md",
            "collection": "canonical",
            "type": "summary",
            "content": "OpenClaw was the historical runtime and has archived credential paths.",
            "score": 95.0,
        },
        {
            "id": "hermes",
            "title": "Current Hermes runtime decision",
            "path": "/canonical/decisions/current-hermes-runtime.md",
            "collection": "semantic_memory",
            "metadata": {"category": "decision"},
            "content": "Current runtime is Hermes; OpenClaw paths are historical/retired and should not be used for current setup.",
            "score": 78.0,
        },
    ]

    _apply_recall_governance_inplace("OpenClaw vs Hermes current runtime", fused)

    assert fused[1]["score"] > fused[0]["score"]


def test_recall_governance_terminal_telegram_authorization_beats_session_key_noise():
    from routes.recall import _apply_recall_governance_inplace

    fused = [
        {
            "id": "session-keys",
            "title": "Session Keys (for sessions_send)",
            "path": "/Users/chrischo/.openclaw/workspace-ellie/AGENTS.md",
            "collection": "knowledge",
            "content": (
                "Session Keys for Telegram sessions_send. Jenna, Liz, Ellie, "
                "Market, and Sage direct Telegram session identifiers."
            ),
            "score": 132.0,
        },
        {
            "id": "market-usage",
            "title": "Market is actively used for brainstorming",
            "collection": "canonical",
            "content": "OpenClaw sage session: Market is actively used for brainstorming rather than being idle.",
            "score": 116.0,
        },
        {
            "id": "semantic_memory:c689ad11cfca1a60",
            "title": "terminal authorization correction",
            "collection": "semantic_memory",
            "metadata": {"category": "correction"},
            "content": (
                "Chris said: 이거 내가 권한준거라 false positive야. "
                "Ellie updated hermes_ops_watchdog.py to allow market and sage "
                "Telegram toolsets to use terminal."
            ),
            "score": 76.0,
        },
    ]

    _apply_recall_governance_inplace(
        "Hermes fleet ops watchdog market sage terminal sensitive telegram tools allowlist authorization",
        fused,
    )
    fused.sort(key=lambda r: r["score"], reverse=True)

    assert fused[0]["id"] == "semantic_memory:c689ad11cfca1a60"
    assert "terminal_telegram_authorization" in fused[0]["governance"]
    assert "terminal_telegram_authorization_noise_penalty" in fused[1]["governance"]


def test_recall_governance_terminal_telegram_authorization_handles_korean_variant():
    from routes.recall import _apply_recall_governance_inplace

    fused = [
        {
            "id": "market-usage",
            "title": "Market is actively used for brainstorming",
            "collection": "canonical",
            "content": "OpenClaw sage session: Market is actively used for brainstorming rather than being idle.",
            "score": 141.0,
        },
        {
            "id": "semantic_memory:c689ad11cfca1a60",
            "title": "terminal authorization correction",
            "collection": "semantic_memory",
            "metadata": {"category": "correction"},
            "content": "내가 권한준거라 false positive야. market sage 텔레그램 terminal 허용 allowlist.",
            "score": 78.0,
        },
    ]

    _apply_recall_governance_inplace("market sage 텔레그램 terminal 권한 내가 허용한 거야?", fused)
    fused.sort(key=lambda r: r["score"], reverse=True)

    assert fused[0]["id"] == "semantic_memory:c689ad11cfca1a60"
    assert "terminal_telegram_authorization" in fused[0]["governance"]


def test_recall_governance_terminal_authorization_does_not_penalize_session_key_lookup():
    from routes.recall import _apply_recall_governance_inplace

    fused = [
        {
            "id": "session-keys",
            "title": "Session Keys (for sessions_send)",
            "path": "/Users/chrischo/.openclaw/workspace-ellie/AGENTS.md",
            "collection": "knowledge",
            "content": "Session Keys for Telegram sessions_send. Claude ACP response relay keys.",
            "score": 132.0,
        }
    ]

    _apply_recall_governance_inplace("What session key should Claude ACP responses relay through?", fused)

    assert "terminal_telegram_authorization_noise_penalty" not in fused[0].get("governance", [])


def test_recall_governance_terminal_telegram_authorization_allowed_by_chris_beats_final_review_noise():
    from routes.recall import _apply_recall_governance_inplace

    fused = [
        {
            "id": "heuristic:0067fb8d26b90e80",
            "title": "erl_extraction",
            "collection": "semantic_memory",
            "content": "Heuristic extraction about market analysis and Chris governance, but no terminal Telegram authorization.",
            "score": 260.0,
        },
        {
            "id": "canonical-memory-hygiene-summary",
            "title": '{"author": "Chris Cho", "body": "Two memory hygiene functions backed by',
            "collection": "canonical",
            "content": "Memory hygiene functions mentioning Chris, market, sage, Telegram, and permissions generically.",
            "score": 220.0,
        },
        {
            "id": "chris_profile_preferences",
            "title": "Chris profile preferences",
            "collection": "canonical",
            "content": "General profile preferences for Chris, with no terminal Telegram authorization correction.",
            "score": 210.0,
        },
        {
            "id": "canonical-screen-time-patterns",
            "title": "Chris screen time patterns across March 14 to March 23, 2026",
            "collection": "canonical",
            "content": "Chris screen time patterns and Telegram usage notes unrelated to terminal authorization.",
            "score": 200.0,
        },
        {
            "id": "author_chris_cho_body_ran_a_6_agent_parallel_source_review_ac",
            "title": '{"author": "Chris Cho", "body": "Ran a 6-agent parallel source review ac',
            "collection": "canonical",
            "content": "A parallel source review mentioning Market and Sage as agents but not the terminal Telegram authorization correction.",
            "score": 190.0,
        },
        {
            "id": "semantic_memory:2590c7b1e60666df",
            "title": "claude_code",
            "collection": "semantic_memory",
            "metadata": {"source_name": "claude_code", "category": "preference"},
            "content": "Chris prefers in-process brain scheduler jobs over external schedulers.",
            "score": 180.0,
        },
        {
            "id": "semantic_memory:c689ad11cfca1a60",
            "title": "terminal authorization correction",
            "collection": "semantic_memory",
            "metadata": {"category": "correction"},
            "content": (
                "Chris said: 이거 내가 권한준거라 false positive야. "
                "Ellie updated hermes_ops_watchdog.py to allow market and sage "
                "Telegram toolsets to use terminal."
            ),
            "score": 76.0,
        },
    ]

    _apply_recall_governance_inplace("market sage terminal Telegram allowed by Chris?", fused)
    fused.sort(key=lambda r: r["score"], reverse=True)

    assert fused[0]["id"] == "semantic_memory:c689ad11cfca1a60"
    assert "terminal_telegram_authorization" in fused[0]["governance"]
    for noise in fused[1:]:
        assert "terminal_telegram_authorization_noise_penalty" in noise.get("governance", [])


def test_recall_governance_terminal_telegram_authorization_allows_concise_policy_atom():
    from routes.recall import _apply_recall_governance_inplace

    fused = [
        {
            "id": "631ab3eee2416904216c405aa7b319e6",
            "title": "chris (14) (part 4)",
            "collection": "canonical",
            "content": (
                "Summarized from documented tech preferences. W15 was a consolidation week "
                "where Chris pushed the brain stack toward stricter verification and clearer agent roles."
            ),
            "score": 260.0,
        },
        {
            "id": "semantic_memory:5add1dca0f5f18aa",
            "title": "mcp",
            "collection": "semantic_memory",
            "content": (
                "Chris explicitly authorized `terminal` in Telegram toolsets for Hermes "
                "`market` and `sage` profiles; Ellie fleet watchdog should treat "
                "market/sage terminal access as allowed policy, not drift."
            ),
            "score": 170.0,
        },
    ]

    _apply_recall_governance_inplace("market sage terminal Telegram allowed by Chris?", fused)
    fused.sort(key=lambda r: r["score"], reverse=True)

    assert fused[0]["id"] == "semantic_memory:5add1dca0f5f18aa"
    assert "terminal_telegram_authorization" in fused[0]["governance"]
    assert "terminal_telegram_authorization_noise_penalty" not in fused[0]["governance"]
    assert "terminal_telegram_authorization_noise_penalty" in fused[1].get("governance", [])


def test_recall_v2_terminal_telegram_authorization_query_deepens_candidate_pool(monkeypatch):
    from routes import recall as recall_route
    from starlette.requests import Request

    recall_route._recall_cache.clear()
    calls: list[dict] = []

    def fake_search_all(query, n, **kwargs):
        calls.append({"query": query, "n": n, **kwargs})
        return {"results": [], "total_candidates": 0, "source_timing": {}}

    monkeypatch.setattr(recall_route.search_unified, "search_all", fake_search_all)

    request = Request(
        {"type": "http", "method": "GET", "path": "/recall/v2", "headers": [], "query_string": b""}
    )
    recall_route.recall_v2(
        request,
        q="market sage terminal Telegram allowed by Chris?",
        n=3,
        rerank=False,
        decay=False,
    )

    assert calls
    assert max(call["n"] for call in calls) >= 40


def test_recall_v2_terminal_telegram_authorization_allowed_by_chris_adds_evidence_rescue_variant(monkeypatch):
    from routes import recall as recall_route
    from starlette.requests import Request

    recall_route._recall_cache.clear()
    calls: list[dict] = []

    def fake_search_all(query, n, **kwargs):
        calls.append({"query": query, "n": n, **kwargs})
        return {"results": [], "total_candidates": 0, "source_timing": {}}

    monkeypatch.setattr(recall_route.search_unified, "search_all", fake_search_all)

    request = Request(
        {"type": "http", "method": "GET", "path": "/recall/v2", "headers": [], "query_string": b""}
    )
    recall_route.recall_v2(
        request,
        q="market sage terminal Telegram allowed by Chris?",
        n=10,
        rerank=False,
        decay=False,
    )

    queries = [call["query"] for call in calls]
    assert "fleet ops watchdog terminal false positive market sage" in queries
    assert "이거 내가 권한준거라 false positive market sage telegram terminal" in queries


# ── Historical-intent narrowing of _is_live_state_query (Sage CR for t_a321da09) ──


def test_is_live_state_query_english_historical_kanban_status_is_false():
    """English explicit historical lookup ("history ... last week") must NOT
    be classified live-state even though "task status" matches a live pattern —
    the user is asking memory for an archived record, not the current board.
    """
    from routes.recall import _is_live_state_query

    assert _is_live_state_query("history of kanban task status from last week") is False


def test_is_live_state_query_korean_historical_kanban_completed_is_false():
    """Korean explicit historical lookup (지난주 ... 기록) must NOT be
    classified live-state even though 칸반.*완료/태스크 matches a live pattern.
    """
    from routes.recall import _is_live_state_query

    assert _is_live_state_query("지난주 칸반 완료 태스크 기록") is False


def test_is_live_state_query_current_status_with_bare_done_is_true():
    """A current-status prompt that ends with bare "done?" is asking whether
    the live task is done right now, not asking memory for archived work.
    Bare English "done" must NOT trigger the historical override on its own —
    only history/archive/past/record/log/last-week style cues should do that.
    """
    from routes.recall import _is_live_state_query

    assert _is_live_state_query("current status of kanban task t_12345678 done?") is True


def test_recall_v2_english_historical_kanban_status_searches_memory(monkeypatch):
    """recall_v2 must invoke search_unified.search_all and NOT short-circuit
    on the live-state path when the English query carries an explicit
    historical lookup intent ("history ... last week")."""
    from routes import recall as recall_route
    from starlette.requests import Request

    recall_route._recall_cache.clear()
    calls: list[dict] = []

    def fake_search_all(query, n, **kwargs):
        calls.append({"query": query, "n": n, **kwargs})
        return {"results": [], "total_candidates": 0, "source_timing": {}}

    monkeypatch.setattr(recall_route.search_unified, "search_all", fake_search_all)

    request = Request(
        {"type": "http", "method": "GET", "path": "/recall/v2", "headers": [], "query_string": b""}
    )
    response = recall_route.recall_v2(
        request,
        q="history of kanban task status from last week",
        n=3,
        rerank=False,
        decay=False,
    )

    assert calls, "historical kanban status query should search instead of short-circuiting"
    assert response.timing.get("live_state_query") is None
    assert response.meta_note != "Live-state/status query — use live tools instead of stale memory recall."


def test_recall_v2_korean_historical_kanban_completed_searches_memory(monkeypatch):
    """recall_v2 must invoke search_unified.search_all and NOT short-circuit
    on the live-state path when the Korean query carries an explicit
    historical lookup intent (지난주 ... 기록)."""
    from routes import recall as recall_route
    from starlette.requests import Request

    recall_route._recall_cache.clear()
    calls: list[dict] = []

    def fake_search_all(query, n, **kwargs):
        calls.append({"query": query, "n": n, **kwargs})
        return {"results": [], "total_candidates": 0, "source_timing": {}}

    monkeypatch.setattr(recall_route.search_unified, "search_all", fake_search_all)

    request = Request(
        {"type": "http", "method": "GET", "path": "/recall/v2", "headers": [], "query_string": b""}
    )
    response = recall_route.recall_v2(
        request,
        q="지난주 칸반 완료 태스크 기록",
        n=3,
        rerank=False,
        decay=False,
    )

    assert calls, "Korean historical kanban query should search instead of short-circuiting"
    assert response.timing.get("live_state_query") is None
    assert response.meta_note != "Live-state/status query — use live tools instead of stale memory recall."


# ── Word-order / separator variants of _is_live_state_query (t_89eaf8a6) ──
# Live spot checks against /recall/v2 for x-agent=liz showed three terse
# phrasings leaking stale memory results instead of short-circuiting. The
# strict _LIVE_STATE_QUERY_PATTERNS missed them because:
#   1. "Kanban task t_<id> status" — the ID slot breaks `\btask\s+status\b`
#   2. "current/status/Kanban/progress" — slashes block `\s+` boundaries
#   3. "kanban progress status current task" — words in the wrong order
# Each test pins one of the three; the historical-exception coverage below
# guards against the token-cluster fallback over-firing on archived lookups
# (the t_a321da09 historical-narrowing fix must keep holding).


def test_is_live_state_query_kanban_task_id_status_word_order_variant():
    """Terse 'Kanban task t_<id> status' is a live-status ask; the ID slot
    between 'task' and 'status' must not break detection."""
    from routes.recall import _is_live_state_query

    assert _is_live_state_query("Kanban task t_12345678 status") is True


def test_is_live_state_query_slash_separated_status_kanban_progress():
    """Slash-delimited 'current/status/Kanban/progress' is a live-status ask
    — non-whitespace separators must normalize to spaces so the existing
    `\\bcurrent\\s+status\\b` pattern still matches."""
    from routes.recall import _is_live_state_query

    assert _is_live_state_query("current/status/Kanban/progress") is True


def test_is_live_state_query_kanban_intent_cluster_reversed_word_order():
    """'kanban progress status current task' clusters every live-state cue
    in the wrong order. The token-cluster fallback must catch it after the
    strict regex misses."""
    from routes.recall import _is_live_state_query

    assert _is_live_state_query("kanban progress status current task") is True


def test_is_live_state_query_word_order_variants_respect_historical_exceptions():
    """Historical/archived intent (history, last week, 지난주, 기록, archived)
    must still suppress live-state on the three new word-order/slash variants
    — the historical guard runs BEFORE the token-cluster fallback so the
    t_a321da09 historical-narrowing fix keeps holding."""
    from routes.recall import _is_live_state_query

    assert _is_live_state_query("history of Kanban task status t_12345678 last week") is False
    assert _is_live_state_query("archived/Kanban/progress/status") is False
    assert _is_live_state_query("kanban progress status current task records last week") is False
    # Pre-existing historical fixtures must keep returning False
    assert _is_live_state_query("history of kanban task status from last week") is False
    assert _is_live_state_query("지난주 칸반 완료 태스크 기록") is False


def test_is_live_state_query_token_cluster_does_not_overfire():
    """The token-cluster fallback must not over-fire on prompts that mention
    kanban or task without status/progress/current/running intent, otherwise
    legitimate definition/setup recalls would be short-circuited."""
    from routes.recall import _is_live_state_query

    # 'kanban' present but no intent token → False (definition/setup queries)
    assert _is_live_state_query("kanban definition explanation") is False
    assert _is_live_state_query("kanban setup instructions") is False
    # Non-kanban context with only one intent token → False (needs ≥2)
    assert _is_live_state_query("current task assignments") is False
    # Pre-existing fixtures the token-cluster path must not flip to True
    assert _is_live_state_query("running local inference decision") is False
    assert _is_live_state_query("complete guide to recall governance") is False
    assert _is_live_state_query("started workflow preference") is False


# ── Bare elliptical status queries (t_e4275737) ──
# Live probe: terse "running now" / "진행 중" / "지금 실행 중" prompts arrive
# without the "what is" prefix the strict English regex requires and without
# the "진행 상황" tail the strict Korean regex requires. They must still
# classify as live-state so recall short-circuits to live tools.


def test_is_live_state_query_bare_english_running_now_is_live_state():
    """Bare elliptical 'running now' / 'running right now' must classify as
    live-state — same intent as 'what is running right now' just without the
    'what is' prefix."""
    from routes.recall import _is_live_state_query

    assert _is_live_state_query("running now") is True
    assert _is_live_state_query("running right now") is True


def test_is_live_state_query_bare_korean_in_progress_is_live_state():
    """Bare elliptical Korean status: '진행 중' (in progress) and
    '지금 실행 중' (running right now) must classify as live-state, paralleling
    the existing '진행 상황' coverage."""
    from routes.recall import _is_live_state_query

    assert _is_live_state_query("진행 중") is True
    assert _is_live_state_query("진행중") is True
    assert _is_live_state_query("지금 실행 중") is True


def test_is_live_state_query_bare_status_patterns_respect_durable_counterexamples():
    """Adding bare-running-now / 진행 중 / 실행 중 patterns must NOT flip
    durable preference / history / from-memory recall queries to live-state.
    The durable counterexamples from the existing suppression coverage stay
    False, and Korean past-tense inflections like '진행 중이었어' (was in
    progress) must not match the new '중' patterns."""
    from routes.recall import _is_live_state_query

    # Pre-existing durable preference counterexamples remain False
    assert _is_live_state_query("durable current preference for kanban status recall governance") is False
    assert _is_live_state_query("Chris current status-control preference from memory") is False
    assert _is_live_state_query("canonical decision about task status governance") is False
    # English token "running" without "now" must not flip to live-state
    assert _is_live_state_query("running local inference decision") is False
    # Korean past-tense '중이었어' / '중이었던' must not match the bare '중' pattern
    assert _is_live_state_query("진행 중이었어 어제 작업 기록") is False
    assert _is_live_state_query("실행 중이었던 과거 작업 기록") is False


# ── Explicit "summary 말고" exclusion (live broad_recommendation probe) ──


def test_is_summary_excluded_query_matches_korean_and_english_cues():
    """Detection covers both 'summary 말고' / '요약 빼고' and English
    'not the summary' / 'without weekly summary' phrasings, and stays False
    for prompts that simply ask for a summary or omit the cue entirely.
    """
    from routes.recall import _is_summary_excluded_query

    assert _is_summary_excluded_query("generic weekly summary 말고") is True
    assert _is_summary_excluded_query("요약 말고 구체적인 결정 보여줘") is True
    assert _is_summary_excluded_query("요약 빼고") is True
    assert _is_summary_excluded_query("not the summary please") is True
    assert _is_summary_excluded_query("without weekly summary") is True
    assert _is_summary_excluded_query("no generic summary") is True
    assert _is_summary_excluded_query("이미지 생성 추천") is False
    assert _is_summary_excluded_query("give me the weekly summary") is False


def test_recall_governance_broad_recommendation_no_generic_summary():
    """Live probe regression for broad_recommendation_no_generic_summary:
    when the prompt explicitly excludes 'generic weekly summary 말고', the
    specific canonical preference must outrank Summary rows even if every
    other candidate happens to be another Summary (so the conditional
    non_summary_topical_exists branch would otherwise skip the penalty)."""
    from routes.recall import _apply_recall_governance_inplace

    fused = [
        {
            "id": "summary_a",
            "title": "Summary",
            "path": "/canonical/summaries/weekly-2026-w20-summary.md",
            "collection": "canonical",
            "type": "weekly-summary",
            "content": (
                "Weekly brain summary: Chris preferences for new tool recommendations, "
                "principles for selection, and overall workflow."
            ),
            "score": 300.0,
        },
        {
            "id": "summary_b",
            "title": "Summary",
            "path": "/canonical/summaries/weekly-2026-w19-summary.md",
            "collection": "canonical",
            "type": "weekly-summary",
            "content": (
                "Weekly brain summary covering preference principles and recommendation "
                "patterns for new tools across the week."
            ),
            "score": 150.0,
        },
        {
            "id": "summary_c",
            "title": "Summary",
            "path": "/canonical/summaries/session-distilled-2026-05-21.md",
            "collection": "canonical",
            "type": "session-summary",
            "content": (
                "Session-distilled summary listing recommendation principles and "
                "preference cues Chris referenced for new tools."
            ),
            "score": 140.0,
        },
        {
            "id": "preference",
            "title": "Tool recommendation principle preference",
            "path": "/canonical/preferences/tool-recommendation-principles.md",
            "collection": "canonical",
            "metadata": {"category": "preference", "review_state": "accepted"},
            "content": (
                "Chris prefers tool recommendations to consult canonical preference "
                "and decision records first; weekly summaries should never dominate."
            ),
            "score": 90.0,
        },
    ]

    _apply_recall_governance_inplace(
        "내 선호에 맞춰 새 도구 추천할 때 어떤 원칙을 먼저 봐야 해? generic weekly summary 말고",
        fused,
    )

    fused.sort(key=lambda r: r["score"], reverse=True)
    assert fused[0]["id"] == "preference", (
        f"specific preference must win over generic summary rows; got order " f"{[r['id'] for r in fused]}"
    )
    for row in fused:
        if row["id"].startswith("summary"):
            assert "explicit_summary_exclusion_penalty" in row.get(
                "governance", []
            ), f"summary row {row['id']} should be tagged with explicit exclusion penalty"


def test_recall_governance_explicit_summary_exclusion_penalty_is_unconditional():
    """Even when no non-summary topical candidate exists in the window (so
    the legacy generic_summary_penalty branch would skip), explicit summary
    exclusion must still penalize summary rows.
    """
    from routes.recall import _apply_recall_governance_inplace

    fused = [
        {
            "id": "only_summary",
            "title": "Summary",
            "path": "/canonical/summaries/weekly-2026-w20-summary.md",
            "collection": "canonical",
            "type": "weekly-summary",
            "content": "Weekly brain summary mentioning recommendation principles for tools.",
            "score": 100.0,
        },
        {
            "id": "metadata_summary",
            "title": "Summary",
            "collection": "canonical",
            "metadata": {
                "source_path": "canonical/summaries/session-distilled-2026-05-21.md",
                "source_name": "session-distilled-2026-05-21.md",
            },
            "content": "Session-distilled summary mentioning recommendation principles for tools.",
            "score": 100.0,
        },
    ]

    _apply_recall_governance_inplace("도구 추천 원칙 summary 말고", fused)
    for row in fused:
        assert "explicit_summary_exclusion_penalty" in row.get("governance", [])
        assert row["score"] < 100.0


# ── Positive summary intent (user explicitly asks for summary/recap/요약) ──


def test_is_positive_summary_intent_query_matches_summary_recap_cues():
    """Detector returns True when the prompt explicitly asks for summaries,
    recaps, or 요약. Returns False when the prompt explicitly excludes
    summaries — exclusion always wins over positive intent."""
    from routes.recall import _is_positive_summary_intent_query

    # Positive intent cues
    assert _is_positive_summary_intent_query("give me the weekly summary") is True
    assert _is_positive_summary_intent_query("주간 요약 보여줘") is True
    assert _is_positive_summary_intent_query("요약 좀 줄래") is True
    assert _is_positive_summary_intent_query("history summary please") is True
    assert _is_positive_summary_intent_query("show me the recap") is True
    assert _is_positive_summary_intent_query("summarize last week") is True
    assert _is_positive_summary_intent_query("brain summary for last sprint") is True
    assert _is_positive_summary_intent_query("weekly summaries from last month") is True

    # Exclusion always wins, even when summary/요약 token is present
    assert _is_positive_summary_intent_query("generic weekly summary 말고") is False
    assert _is_positive_summary_intent_query("요약 말고 구체적인 결정 보여줘") is False
    assert _is_positive_summary_intent_query("요약 빼고") is False
    assert _is_positive_summary_intent_query("not the summary please") is False
    assert _is_positive_summary_intent_query("without weekly summary") is False
    assert _is_positive_summary_intent_query("no generic summary") is False

    # Unrelated prompts stay False
    assert _is_positive_summary_intent_query("이미지 생성 추천") is False
    assert _is_positive_summary_intent_query("tool recommendation principle") is False
    assert _is_positive_summary_intent_query("") is False


def test_recall_governance_live_validation_music_tts_suppresses_offtopic_manual_note():
    from routes.recall import _apply_recall_governance_inplace

    fused = [
        {
            "id": "logo",
            "title": "mcp",
            "collection": "semantic_memory",
            "content": "Logo feedback: Chris wants personal brand logo, not generic premium-tech symbol; avoid abstract marks.",
            "score": 110.0,
        },
        {
            "id": "brain-contract",
            "title": "Brain contract (2026-04-24)",
            "collection": "knowledge",
            "content": "Brain is the primary durable memory store. It mentions local models and no paid API constraints for Brain operations.",
            "score": 185.0,
        },
        {
            "id": "music-tts",
            "title": "Music and TTS billing constraints",
            "collection": "semantic_memory",
            "metadata": {"category": "preference"},
            "content": "Chris is cost-conscious for music generation and TTS: avoid local model hosting and avoid new paid API spend; use existing subscriptions/integrations.",
            "score": 130.0,
        },
    ]

    _apply_recall_governance_inplace(
        "music generation TTS local model no new paid API Chris constraint", fused
    )

    assert fused[2]["score"] > fused[0]["score"]
    assert fused[2]["score"] > fused[1]["score"]
    assert "budget_local_cloud_constraint" in fused[2]["governance"]
    assert "budget_offtopic_penalty" in fused[0]["governance"]
    assert "brain_contract_offtopic_penalty" in fused[1]["governance"]


def test_recall_governance_mixed_language_music_tts_suppresses_brain_failure_note():
    from routes.recall import _apply_recall_governance_inplace

    fused = [
        {
            "id": "korean-name-failure",
            "title": "## What happened - Korean name Brain failure",
            "collection": "experience",
            "content": "What happened: Chris asked whether his Korean name was stored in Brain. brain_recall failed to surface the correct personal fact.",
            "score": 240.0,
        },
        {
            "id": "cost-pref",
            "title": "memory_nudge_pattern",
            "collection": "semantic_memory",
            "metadata": {"category": "preference"},
            "content": "Chris is cost-conscious and prefers existing subscriptions and integrations over new paid API spend or local model hosting for music and TTS.",
            "score": 140.0,
        },
    ]

    _apply_recall_governance_inplace("Chris 음악 TTS local model no paid API 제약", fused)

    assert fused[1]["score"] > fused[0]["score"]
    assert "brain_failure_note_penalty" in fused[0]["governance"]


def test_recall_governance_calendar_tooling_penalizes_business_automation_noise():
    from routes.recall import _apply_recall_governance_inplace

    fused = [
        {
            "id": "business-plan",
            "title": "semantic",
            "collection": "obsidian",
            "content": "Automation Platform for Small Businesses and Individuals. SaaS startup planning for automation workflows.",
            "score": 205.0,
        },
        {
            "id": "tooling",
            "title": "Primary Tooling Choices",
            "collection": "knowledge",
            "content": "Reminders: `apple-reminders` primary. Calendar: `macos-calendar` primary local calendar, `google-workspace-mcp` for Google side.",
            "score": 166.0,
        },
    ]

    _apply_recall_governance_inplace("Apple Calendar Reminders Chris preferred tools automation", fused)

    assert fused[1]["score"] > fused[0]["score"]
    assert "calendar_tooling_offtopic_penalty" in fused[0]["governance"]
    assert "primary_tooling_choice" in fused[1]["governance"]


def test_recall_governance_historical_runtime_penalizes_live_state_snapshot():
    from routes.recall import _apply_recall_governance_inplace

    fused = [
        {
            "id": "live-state",
            "title": "Manual focus items (10) (part 1)",
            "collection": "canonical",
            "metadata": {
                "document_title": "Manual focus items (10) (part 1)",
                "document_type": "canonical-note",
                "source_path": "/Users/chrischo/server/knowledge/canonical/live_state/active_goals.md",
            },
            "score": 165.0,
        },
        {
            "id": "runtime",
            "title": "Hermes vs OpenClaw historical runtime distinction",
            "collection": "canonical",
            "metadata": {"category": "decision", "review_state": "accepted"},
            "content": "Hermes is the current runtime. OpenClaw is the historical predecessor; OpenClaw paths and runtime assumptions are retired.",
            "score": 100.0,
        },
    ]

    _apply_recall_governance_inplace("Hermes vs OpenClaw historical runtime distinction", fused)

    assert fused[1]["score"] > fused[0]["score"]
    assert "live_state_snapshot_penalty" in fused[0]["governance"]


def test_recall_governance_broad_tool_recommendation_penalizes_distilled_brain_analysis():
    from routes.recall import _apply_recall_governance_inplace

    fused = [
        {
            "id": "brain-analysis",
            "title": "Reasoning",
            "collection": "canonical",
            "metadata": {
                "id": "dist_brain_analysis_123",
                "document_type": "distilled-note",
                "source_path": "/Users/chrischo/server/knowledge/distilled/decisions/brain_analysis_123.md",
            },
            "score": 154.0,
        },
        {
            "id": "ops-pref",
            "title": "Chris operational preferences for automation and recommendations",
            "collection": "canonical",
            "metadata": {"category": "preference", "review_state": "accepted"},
            "content": "Chris prefers useful tool recommendations with low noise, high leverage, and evidence from operational preferences.",
            "score": 120.0,
        },
    ]

    _apply_recall_governance_inplace(
        "recommend a useful tool for Chris given his preferences no-noise max-help", fused
    )

    assert fused[1]["score"] > fused[0]["score"]
    assert "distilled_brain_analysis_penalty" in fused[0]["governance"]


def test_recall_governance_broad_tool_recommendation_penalizes_openclaw_summary_noise():
    from routes.recall import _apply_recall_governance_inplace

    fused = [
        {
            "id": "openclaw-summary",
            "title": "Chris operational preferences for OpenClaw automation, browser control, and migration",
            "collection": "canonical",
            "content": (
                "## Summary This consolidated page captures Chris's operational rules for how "
                "OpenClaw should handle browser automation, gateway-sensitive actions, migration/setup "
                "portability, progress reporting, and standing workflow preferences."
            ),
            "score": 149.0,
        },
        {
            "id": "candidate",
            "title": "claude_code",
            "collection": "canonical",
            "content": (
                "Chris uses a concrete recent gap test before building new tooling; candidate "
                "tool rows are valid evidence for useful tool recommendations."
            ),
            "score": 145.0,
        },
        {
            "id": "pref",
            "title": "Tool recommendation principle preference",
            "collection": "canonical",
            "metadata": {"category": "preference", "review_state": "accepted"},
            "content": "Chris prefers useful tool recommendations with low noise, max help, and concrete evidence.",
            "score": 120.0,
        },
    ]

    _apply_recall_governance_inplace(
        "recommend a useful tool for Chris given his preferences no-noise max-help", fused
    )

    assert fused[1]["score"] > fused[0]["score"]
    assert fused[2]["score"] > fused[0]["score"]
    assert "broad_tool_recommendation_noise_penalty" in fused[0]["governance"]
    assert "broad_tool_recommendation_noise_penalty" not in fused[1].get("governance", [])
    assert "broad_tool_recommendation_noise_penalty" not in fused[2].get("governance", [])


def test_recall_governance_positive_summary_intent_skips_summary_penalties():
    """When the prompt explicitly asks for summaries/recaps/요약, generic
    Summary rows must NOT receive generic_summary_penalty or
    explicit_summary_exclusion_penalty — those are exactly the rows the
    user requested."""
    from routes.recall import _apply_recall_governance_inplace

    fused = [
        {
            "id": "summary_a",
            "title": "Summary",
            "path": "/canonical/summaries/weekly-2026-w20-summary.md",
            "collection": "canonical",
            "type": "weekly-summary",
            "content": (
                "Weekly brain summary: Chris preferences for new tool recommendations, "
                "principles for selection, and overall workflow."
            ),
            "score": 100.0,
        },
        {
            "id": "preference_a",
            "title": "Tool recommendation principle preference",
            "path": "/canonical/preferences/tool-recommendation-principles.md",
            "collection": "canonical",
            "metadata": {"category": "preference", "review_state": "accepted"},
            "content": (
                "Chris prefers tool recommendations to consult canonical preference "
                "records first; weekly summaries should never dominate."
            ),
            "score": 90.0,
        },
    ]

    _apply_recall_governance_inplace(
        "give me last week's weekly summary of tool recommendation principles",
        fused,
    )

    summary_row = next(r for r in fused if r["id"] == "summary_a")
    governance = summary_row.get("governance", [])
    assert (
        "generic_summary_penalty" not in governance
    ), f"positive summary intent must not penalize summary rows; got {governance}"
    assert (
        "explicit_summary_exclusion_penalty" not in governance
    ), f"positive intent is not exclusion; got {governance}"


def test_recall_governance_korean_summary_intent_skips_generic_summary_penalty():
    """Korean 요약 prompts must allow generic Summary rows through without
    the generic_summary_penalty, mirroring the English positive-intent path."""
    from routes.recall import _apply_recall_governance_inplace

    fused = [
        {
            "id": "summary_korean",
            "title": "Summary",
            "path": "/canonical/summaries/weekly-2026-w20-summary.md",
            "collection": "canonical",
            "type": "weekly-summary",
            "content": (
                "주간 brain summary: tool recommendation principles and preference "
                "decisions covered during the week."
            ),
            "score": 100.0,
        },
        {
            "id": "preference_korean",
            "title": "Tool recommendation principle preference",
            "path": "/canonical/preferences/tool-recommendation-principles.md",
            "collection": "canonical",
            "metadata": {"category": "preference", "review_state": "accepted"},
            "content": "Chris prefers canonical tool recommendation principles first.",
            "score": 90.0,
        },
    ]

    _apply_recall_governance_inplace("지난주 도구 추천 원칙 주간 요약 보여줘", fused)

    summary_row = next(r for r in fused if r["id"] == "summary_korean")
    governance = summary_row.get("governance", [])
    assert "generic_summary_penalty" not in governance
    assert "explicit_summary_exclusion_penalty" not in governance


def test_recall_governance_explicit_exclusion_wins_over_positive_intent():
    """Explicit exclusion cues ('summary 말고', 'not the summary') beat
    positive intent cues. Generic Summary rows must still get
    explicit_summary_exclusion_penalty even when 'summary'/'요약' appear."""
    from routes.recall import _apply_recall_governance_inplace

    fused = [
        {
            "id": "summary_excluded",
            "title": "Summary",
            "path": "/canonical/summaries/weekly-2026-w20-summary.md",
            "collection": "canonical",
            "type": "weekly-summary",
            "content": "Weekly brain summary listing tool recommendation principles.",
            "score": 100.0,
        },
    ]

    _apply_recall_governance_inplace(
        "weekly summary 말고 도구 추천 원칙 보여줘",
        fused,
    )

    governance = fused[0].get("governance", [])
    assert (
        "explicit_summary_exclusion_penalty" in governance
    ), f"exclusion must still win when both cues are present; got {governance}"
    assert fused[0]["score"] < 100.0


# ── Duplicate/noisy recall result collapse ───────────────────────────────


def test_retrieval_quality_filter_collapses_near_duplicate_brain_quality_rows():
    """Server raw recall should not return several phrasings of the same
    Brain-quality preference together. This guards /recall/v2 before Hermes
    provider prefetch formats the memory-context.
    """
    from routes.recall import _apply_retrieval_quality_filter, _sort_and_diversify

    fused = [
        {
            "id": "old_semantic",
            "path": "/atoms/a",
            "title": "Brain eval preference",
            "collection": "semantic_memory",
            "content": "Chris wants Brain fine tuning judged by measurable eval score improvements.",
            "score": 120.0,
        },
        {
            "id": "canonical_truth",
            "path": "/canonical/brain-quality.md",
            "title": "Brain quality decision",
            "collection": "canonical",
            "content": "Brain fine-tuning should improve measurable eval-score improvements, not vibes.",
            "score": 110.0,
        },
        {
            "id": "specific_other",
            "path": "/canonical/live-state.md",
            "title": "Brain live-state suppression",
            "collection": "canonical",
            "content": "Live status and quota questions should use live tools instead of stale memory prefetch.",
            "score": 90.0,
        },
    ]

    out = _apply_retrieval_quality_filter(
        "Brain recall quality should improve eval score and avoid noisy duplicate prefetch",
        _sort_and_diversify(fused, top_window=5),
    )

    ids = [row["id"] for row in out]
    assert ids.count("old_semantic") + ids.count("canonical_truth") == 1
    assert "specific_other" in ids


# ── retrieval quality governance ───────────────────────────────────────


def test_retrieval_quality_filter_collapses_eval_score_duplicate_memories():
    from routes.recall import _apply_retrieval_quality_filter

    fused = [
        {
            "id": "semantic-old",
            "collection": "semantic_memory",
            "score": 95.0,
            "title": "Brain eval preference",
            "content": "Chris wants Brain fine tuning to be judged by measurable eval score improvements.",
            "metadata": {"category": "preference"},
        },
        {
            "id": "canonical-new",
            "collection": "canonical",
            "score": 70.0,
            "title": "Brain quality decision",
            "content": "Brain fine-tuning work should improve measurable eval score improvements, not vibes.",
            "metadata": {"category": "decision", "review_state": "accepted"},
        },
    ]

    out = _apply_retrieval_quality_filter("브레인 검색품질 평가 점수 개선", fused)

    assert len(out) == 1
    assert out[0]["id"] == "canonical-new"


def test_retrieval_quality_filter_suppresses_generic_brain_infra_summaries():
    from routes.recall import _apply_retrieval_quality_filter

    fused = [
        {
            "id": "summary",
            "collection": "distilled",
            "score": 99.0,
            "title": "Summary",
            "path": "weekly/2026-W20.md",
            "content": "Knowledge Gap Bridge: Brain system dependency. Brain depends on FastAPI brain-server and native Qdrant.",
        },
        {
            "id": "specific",
            "collection": "canonical",
            "score": 60.0,
            "title": "Brain prefetch quality",
            "content": "Brain memory context should avoid noise and duplicate recall blocks.",
            "metadata": {"category": "preference", "review_state": "accepted"},
        },
    ]

    out = _apply_retrieval_quality_filter("Brain memory context noise prefetch", fused)

    assert [r["id"] for r in out] == ["specific"]


def test_retrieval_quality_filter_keeps_requested_brain_subsystem_evidence():
    from routes.recall import _apply_retrieval_quality_filter

    fused = [
        {
            "id": "decide-specific",
            "collection": "canonical",
            "score": 70.0,
            "title": "brain_decide retrieval quality",
            "content": "brain_decide should share the retrieval quality filter with raw recall evidence.",
            "metadata": {"category": "decision", "review_state": "accepted"},
        },
        {
            "id": "other",
            "collection": "canonical",
            "score": 60.0,
            "title": "Brain prefetch quality",
            "content": "Brain prefetch should avoid duplicate memory context blocks.",
            "metadata": {"category": "preference", "review_state": "accepted"},
        },
    ]

    out = _apply_retrieval_quality_filter("brain_decide retrieval quality", fused)

    assert [r["id"] for r in out] == ["decide-specific", "other"]


def test_retrieval_quality_filter_does_not_treat_generic_memory_query_as_brain_quality():
    from routes.recall import _apply_retrieval_quality_filter

    fused = [
        {
            "id": "boston-summary",
            "collection": "distilled",
            "score": 90.0,
            "title": "Boston trip weekly summary",
            "path": "weekly/2026-W18.md",
            "content": "Weekly Summary: Chris planned a Boston trip and captured travel notes.",
        }
    ]

    out = _apply_retrieval_quality_filter("memory of my Boston trip", fused)

    assert [r["id"] for r in out] == ["boston-summary"]


def test_retrieval_quality_filter_keeps_boston_trip_context_summary():
    from routes.recall import _apply_retrieval_quality_filter

    fused = [
        {
            "id": "boston-context",
            "collection": "distilled",
            "score": 90.0,
            "title": "Boston trip weekly summary",
            "path": "weekly/2026-W18.md",
            "content": "Weekly Summary: Chris planned a Boston trip and captured travel context.",
        }
    ]

    out = _apply_retrieval_quality_filter("Boston trip context", fused)

    assert [r["id"] for r in out] == ["boston-context"]


def test_retrieval_quality_filter_keeps_song_quality_notes_summary():
    from routes.recall import _apply_retrieval_quality_filter

    fused = [
        {
            "id": "song-quality",
            "collection": "distilled",
            "score": 90.0,
            "title": "Song quality notes weekly summary",
            "path": "weekly/2026-W18.md",
            "content": "Weekly Summary: Chris captured song quality notes and mix feedback.",
        }
    ]

    out = _apply_retrieval_quality_filter("song quality notes", fused)

    assert [r["id"] for r in out] == ["song-quality"]


def test_retrieval_quality_filter_suppresses_generic_brain_summary_for_quality_query():
    from routes.recall import _apply_retrieval_quality_filter

    fused = [
        {
            "id": "summary",
            "collection": "distilled",
            "score": 99.0,
            "title": "W20 weekly brain summary",
            "path": "weekly/2026-W20.md",
            "content": "Knowledge Gap Bridge: Brain system dependency. Brain depends on FastAPI brain-server.",
        },
        {
            "id": "specific",
            "collection": "canonical",
            "score": 60.0,
            "title": "Brain retrieval quality",
            "content": "Brain retrieval quality should suppress stale generic infra summaries.",
            "metadata": {"category": "preference", "review_state": "accepted"},
        },
    ]

    out = _apply_retrieval_quality_filter("Brain retrieval quality", fused)

    assert [r["id"] for r in out] == ["specific"]


def test_brain_decide_marker_still_counts_as_brain_quality_query():
    from routes.recall import _is_brain_quality_query

    assert _is_brain_quality_query("brain_decide") is True


def test_retrieval_quality_filter_keeps_summary_for_explicit_summary_query():
    from routes.recall import _apply_retrieval_quality_filter

    fused = [
        {
            "id": "summary",
            "collection": "distilled",
            "score": 99.0,
            "title": "Summary",
            "path": "weekly/2026-W20.md",
            "content": "Knowledge Gap Bridge: Brain system dependency. Brain depends on FastAPI brain-server.",
        }
    ]

    out = _apply_retrieval_quality_filter("Brain system 요약", fused)

    assert [r["id"] for r in out] == ["summary"]


def test_recall_batch_uses_shared_quality_filter_and_live_state_gate(monkeypatch):
    from routes import recall as recall_mod

    calls: list[dict] = []

    def _fake_search(query, limit, **kw):
        calls.append({"query": query, "limit": limit, "kw": kw})
        return {
            "results": [
                {
                    "id": "summary",
                    "collection": "distilled",
                    "score": 99.0,
                    "title": "Summary",
                    "path": "weekly/2026-W20.md",
                    "content": "Knowledge Gap Bridge: Brain system dependency. Brain depends on FastAPI brain-server.",
                },
                {
                    "id": "specific",
                    "collection": "canonical",
                    "score": 60.0,
                    "title": "Brain prefetch quality",
                    "content": "Brain memory context should avoid noise and duplicate recall blocks.",
                    "metadata": {"category": "preference", "review_state": "accepted"},
                },
            ]
        }

    monkeypatch.setattr(recall_mod.search_unified, "search_all", _fake_search)

    class _Req:
        queries = ["Brain memory context noise", "current kanban task status"]
        n = 3
        rerank = True
        decay = True
        agent = "test"

    out = recall_mod.recall_batch.__wrapped__(_Req(), _Req())

    by_query = {entry["query"]: entry for entry in out["results"]}
    assert [h["id"] for h in by_query["Brain memory context noise"]["hits"]] == ["specific"]
    assert by_query["current kanban task status"]["hits"] == []
    assert "Live-state/status" in by_query["current kanban task status"]["meta_note"]
    assert calls and calls[0]["limit"] == 6
