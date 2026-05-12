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
