"""Unit tests for search_unified service helpers.

First slice (2026-05-12) of the search_unified.search_all 1320-line split:
  - _apply_bilingual_expansion
  - _apply_ontology_expansion

These helpers were lifted from the search_all preamble. Tests pin the
exact contract (return shape, source_timing key set, sidecar-vs-inline
mode behavior) so the next stages of the split can verify byte-equal
behavior.
"""

from __future__ import annotations

import sys
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


# ── _compose_rrf_inputs ──────────────────────────────────────────────


def _r(n):
    """Build n placeholder result dicts."""
    return [{"id": f"r_{i}"} for i in range(n)]


def test_compose_rrf_inputs_all_sources_present():
    """All 7 sources populated → 7 source_lists in canonical order +
    7 weights using default intent_boost values."""
    import search_unified

    rag, can, obs, graph, fts, gpre, rap = _r(3), _r(2), _r(1), _r(1), _r(1), _r(1), _r(1)
    sl, tw = search_unified._compose_rrf_inputs(rag, can, obs, graph, fts, gpre, rap, {})
    assert len(sl) == 7
    assert sl == [rag, can, obs, graph, fts, gpre, rap]
    assert tw == [0.9, 1.0, 0.6, 0.5, 0.4, 0.7, 0.85]


def test_compose_rrf_inputs_drops_empty_lists():
    """Empty source lists are excluded from both arrays in parallel."""
    import search_unified

    can = _r(2)
    fts = _r(1)
    sl, tw = search_unified._compose_rrf_inputs([], can, [], [], fts, [], [], {})
    assert sl == [can, fts]
    assert tw == [1.0, 0.4]


def test_compose_rrf_inputs_intent_boost_applied_to_rag():
    """intent_boost["rag"] = 2.0 → rag weight = 0.9 * 2.0 = 1.8."""
    import search_unified

    sl, tw = search_unified._compose_rrf_inputs(_r(1), [], [], [], [], [], [], {"rag": 2.0})
    assert sl == [[{"id": "r_0"}]]
    assert tw == [1.8]


def test_compose_rrf_inputs_intent_boost_applied_to_canonical_and_raptor():
    """intent_boost["canonical"] is multiplied into BOTH canonical (1.0*)
    and raptor (0.85*) weights — raptor inherits canonical trust."""
    import search_unified

    sl, tw = search_unified._compose_rrf_inputs([], _r(1), [], [], [], [], _r(1), {"canonical": 2.0})
    assert len(sl) == 2
    assert tw == [2.0, 1.7]  # 1.0*2.0, 0.85*2.0


def test_compose_rrf_inputs_intent_boost_applied_to_graph_sources():
    """graph + graph_prefetch both pick up intent_boost["graph"]."""
    import pytest
    import search_unified

    sl, tw = search_unified._compose_rrf_inputs([], [], [], _r(1), [], _r(1), [], {"graph": 1.5})
    assert len(sl) == 2
    # Float precision: 0.7 * 1.5 = 1.0499999999999998 — use approx.
    assert tw == [pytest.approx(0.75), pytest.approx(1.05)]


def test_compose_rrf_inputs_obsidian_and_fts_have_no_intent_boost():
    """Obsidian (0.6) and FTS (0.4) weights are fixed — no intent_boost lookup."""
    import search_unified

    sl, tw = search_unified._compose_rrf_inputs(
        [], [], _r(1), [], _r(1), [], [], {"obsidian": 999, "fts": 999}
    )
    assert tw == [0.6, 0.4]


def test_compose_rrf_inputs_empty_input_returns_empty():
    """All sources empty → empty source_lists + empty trust_weights."""
    import search_unified

    sl, tw = search_unified._compose_rrf_inputs([], [], [], [], [], [], [], {})
    assert sl == []
    assert tw == []


def test_compose_rrf_inputs_default_boost_is_one():
    """Missing intent_boost key defaults to 1.0 — weights match the bare
    constants (0.9, 1.0, 0.5, 0.7, 0.85)."""
    import search_unified

    sl, tw = search_unified._compose_rrf_inputs(_r(1), _r(1), [], _r(1), [], _r(1), _r(1), {})
    assert tw == [0.9, 1.0, 0.5, 0.7, 0.85]


# ── _matches_entity ──────────────────────────────────────────────────


def test_matches_entity_finds_in_metadata_agent():
    import search_unified

    r = {"metadata": {"agent": "Jenna"}, "content": "lorem"}
    assert search_unified._matches_entity(r, "jenna") is True


def test_matches_entity_finds_in_metadata_service():
    import search_unified

    r = {"metadata": {"service": "qdrant"}}
    assert search_unified._matches_entity(r, "qdrant") is True


def test_matches_entity_finds_in_path():
    import search_unified

    r = {"path": "canonical/openclaw/_profile.md"}
    assert search_unified._matches_entity(r, "openclaw") is True


def test_matches_entity_finds_in_title():
    import search_unified

    r = {"title": "OpenClaw Jenna brief"}
    assert search_unified._matches_entity(r, "jenna") is True


def test_matches_entity_finds_in_first_200_of_content():
    """Only the first 200 chars of content are scanned (substring at idx 100
    counts; substring at idx 250 does NOT)."""
    import search_unified

    short_match = {"content": "x" * 100 + "JENNA" + "y" * 50}
    far_match = {"content": "x" * 250 + "JENNA"}
    assert search_unified._matches_entity(short_match, "jenna") is True
    assert search_unified._matches_entity(far_match, "jenna") is False


def test_matches_entity_returns_false_on_no_match():
    import search_unified

    r = {"path": "irrelevant", "content": "foo"}
    assert search_unified._matches_entity(r, "openclaw") is False


def test_matches_entity_handles_missing_fields():
    """An empty result row (no metadata, no path, no content) must not
    crash; substring scan on empty haystack returns False."""
    import search_unified

    assert search_unified._matches_entity({}, "x") is False
    assert search_unified._matches_entity({"metadata": {}}, "x") is False


def test_matches_entity_uses_entity_lower_directly():
    """The helper does NOT lowercase the entity itself — caller is
    responsible. This matches the byte-equal pre-extraction behavior
    (search_all lowercases once before the loop)."""
    import search_unified

    r = {"path": "Jenna"}
    # Caller passes already-lowered "jenna" → match
    assert search_unified._matches_entity(r, "jenna") is True
    # Caller passes uppercase → won't match (haystack is lowered to "jenna")
    assert search_unified._matches_entity(r, "JENNA") is False


# ── _apply_entity_filter_inplace ─────────────────────────────────────


def test_entity_filter_filters_each_list_inplace():
    import search_unified

    rag = [{"path": "openclaw"}, {"path": "other"}]
    canonical = [{"title": "OpenClaw notes"}, {"title": "tax docs"}]
    obsidian: list[dict] = []

    search_unified._apply_entity_filter_inplace("openclaw", rag, canonical, obsidian)
    assert rag == [{"path": "openclaw"}]
    assert canonical == [{"title": "OpenClaw notes"}]
    assert obsidian == []


def test_entity_filter_preserves_list_identity():
    """Caller's list reference must not change — other code paths may hold
    the same reference and observe the filter."""
    import search_unified

    rag = [{"path": "openclaw"}, {"path": "other"}]
    ref = rag
    search_unified._apply_entity_filter_inplace("openclaw", rag)
    assert ref is rag  # same list object
    assert rag == [{"path": "openclaw"}]


def test_entity_filter_none_or_empty_is_noop():
    """No entity → all lists pass through untouched."""
    import search_unified

    rag = [{"path": "x"}, {"path": "y"}]
    snapshot = list(rag)
    search_unified._apply_entity_filter_inplace(None, rag)
    assert rag == snapshot
    search_unified._apply_entity_filter_inplace("", rag)
    assert rag == snapshot


def test_entity_filter_case_insensitive():
    """Caller passes 'OPENCLAW' → still matches 'openclaw' rows."""
    import search_unified

    rag = [{"path": "openclaw/x"}, {"path": "other"}]
    search_unified._apply_entity_filter_inplace("OPENCLAW", rag)
    assert rag == [{"path": "openclaw/x"}]


# ── _is_broad_query ──────────────────────────────────────────────────


def test_is_broad_query_long_token_count_is_broad():
    """A 5+ token query is broad regardless of keyword presence."""
    import search_unified

    assert search_unified._is_broad_query("one two three four five") is True


def test_is_broad_query_short_no_keyword_is_narrow():
    """Short query with no broad keyword → False (skip RAPTOR)."""
    import search_unified

    # 4 tokens, no broad keyword → False (boundary case)
    assert search_unified._is_broad_query("what time is it") is False
    assert search_unified._is_broad_query("port 8791?") is False
    assert search_unified._is_broad_query("fix bug") is False


def test_is_broad_query_keyword_triggers_even_when_short():
    """Each broad keyword triggers True regardless of token count."""
    import search_unified

    for kw in ("overall", "summary", "pattern", "compare", "philosophy"):
        assert search_unified._is_broad_query(kw) is True, f"keyword {kw!r} did not trigger"


def test_is_broad_query_multi_word_keyword_substring():
    """Multi-word phrases like 'state of' must match as substring,
    not require token equality."""
    import search_unified

    assert search_unified._is_broad_query("state of the brain") is True
    assert search_unified._is_broad_query("what is chris up to") is True
    assert search_unified._is_broad_query("how does chris debug") is True


def test_is_broad_query_case_insensitive():
    """Heuristic must lower-case the query before comparing."""
    import search_unified

    assert search_unified._is_broad_query("PATTERN") is True
    assert search_unified._is_broad_query("Overall State") is True


def test_is_broad_query_empty_and_none_inputs():
    """Empty / None queries fall through to False."""
    import search_unified

    assert search_unified._is_broad_query("") is False
    assert search_unified._is_broad_query(None) is False  # type: ignore[arg-type]


def test_is_broad_query_exactly_4_tokens_no_keyword_is_narrow():
    """The >4 boundary: 4-token query without keyword is NOT broad."""
    import search_unified

    assert search_unified._is_broad_query("a b c d") is False  # 4 tokens
    assert search_unified._is_broad_query("a b c d e") is True  # 5 tokens


def test_is_broad_query_keyword_list_complete():
    """Pin the exact broad-keyword tuple — adding/removing keywords here
    is a deliberate behavior change."""
    import search_unified

    assert search_unified._BROAD_QUERY_KEYWORDS == (
        "overall",
        "pattern",
        "summary",
        "history",
        "philosophy",
        "approach",
        "compare",
        "difference",
        "trend",
        "evolution",
        "strategy",
        "state of",
        "what is chris",
        "how does chris",
    )


# ── _build_rag_where_clause ──────────────────────────────────────────


def test_rag_where_no_input_returns_raw_dump_exclusion():
    """No caller where + no source_type → exclude raw dumps by default."""
    import search_unified

    out = search_unified._build_rag_where_clause(None, None)
    assert out == {"type": {"$nin": list(search_unified._RAW_DUMP_TYPES)}}


def test_rag_where_caller_where_alone_gets_raw_exclude_anded():
    """Caller where + no source_type → AND the raw exclusion on."""
    import search_unified

    caller = {"domain": {"$eq": "infra"}}
    out = search_unified._build_rag_where_clause(caller, None)
    assert out == {
        "$and": [
            {"domain": {"$eq": "infra"}},
            {"type": {"$nin": list(search_unified._RAW_DUMP_TYPES)}},
        ]
    }


def test_rag_where_source_type_alone_is_eq_clause():
    """No caller where + source_type → just the eq clause (no raw exclusion,
    caller explicitly scoped)."""
    import search_unified

    out = search_unified._build_rag_where_clause(None, "note")
    assert out == {"type": {"$eq": "note"}}


def test_rag_where_source_type_with_caller_where_anded():
    """Caller where + source_type → $and both, NO raw exclusion."""
    import search_unified

    caller = {"agent": {"$eq": "jenna"}}
    out = search_unified._build_rag_where_clause(caller, "note")
    assert out == {
        "$and": [
            {"agent": {"$eq": "jenna"}},
            {"type": {"$eq": "note"}},
        ]
    }


def test_rag_where_copies_caller_where_not_referenced():
    """The helper must NOT mutate the caller's where dict by reference."""
    import search_unified

    caller = {"k": "v"}
    out = search_unified._build_rag_where_clause(caller, None)
    # Helper made a defensive copy → original is unchanged
    assert caller == {"k": "v"}
    # Output references its own dict in the $and slot
    assert out["$and"][0] is not caller


def test_rag_where_empty_dict_caller_treated_as_no_where():
    """An empty dict is falsy → treated like None and the clause is
    returned directly (no $and wrapping)."""
    import search_unified

    out = search_unified._build_rag_where_clause({}, None)
    assert out == {"type": {"$nin": list(search_unified._RAW_DUMP_TYPES)}}


def test_rag_where_raw_dump_types_complete():
    """Pin the exact raw-dump exclusion list — adding/removing entries
    here is a deliberate behavior change."""
    import search_unified

    assert search_unified._RAW_DUMP_TYPES == (
        "raw-openclaw_session",
        "raw-claude_code_session",
        "raw-browser",
        "raw-git_activity",
        "raw-screen_time",
    )


# ── _apply_bilingual_expansion ───────────────────────────────────────


def test_bilingual_in_process_uses_first_variant_as_primary(monkeypatch):
    """When _RAG_IN_PROCESS is True and _rag_search.expand_query returns
    multiple variants, helper sets query=variants[0] and stashes the rest
    on bilingual_variants (capped at 2 entries)."""
    import search_unified

    monkeypatch.setattr(search_unified, "_RAG_IN_PROCESS", True)

    class _FakeRagSearch:
        @staticmethod
        def expand_query(q):
            return ["english variant", "korean variant", "third variant", "fourth variant"]

    monkeypatch.setattr(search_unified, "_rag_search", _FakeRagSearch)

    q, variants = search_unified._apply_bilingual_expansion("how are you")
    assert q == "english variant"
    # First entry consumed, then [korean, third, fourth] filtered to first 2
    assert variants == ["korean variant", "third variant"]


def test_bilingual_no_in_process_short_circuits(monkeypatch):
    """When _RAG_IN_PROCESS is False, helper does NOT call expand_query
    and returns the query unchanged with an empty variants list."""
    import search_unified

    monkeypatch.setattr(search_unified, "_RAG_IN_PROCESS", False)

    class _FakeRagSearch:
        @staticmethod
        def expand_query(q):
            raise AssertionError("expand_query must not be called when _RAG_IN_PROCESS=False")

    monkeypatch.setattr(search_unified, "_rag_search", _FakeRagSearch)

    q, variants = search_unified._apply_bilingual_expansion("hello")
    assert q == "hello"
    assert variants == []


def test_bilingual_expand_query_exception_swallowed(monkeypatch):
    """A RAG expansion exception must not propagate — return the original
    query with empty variants."""
    import search_unified

    monkeypatch.setattr(search_unified, "_RAG_IN_PROCESS", True)

    class _FakeRagSearch:
        @staticmethod
        def expand_query(q):
            raise RuntimeError("rag down")

    monkeypatch.setattr(search_unified, "_rag_search", _FakeRagSearch)

    q, variants = search_unified._apply_bilingual_expansion("hello")
    assert q == "hello"
    assert variants == []


def test_bilingual_single_variant_returned_no_alternates(monkeypatch):
    """If expand_query returns a single-element list, no alternates are
    produced and the query is left unchanged (len > 1 gate)."""
    import search_unified

    monkeypatch.setattr(search_unified, "_RAG_IN_PROCESS", True)

    class _FakeRagSearch:
        @staticmethod
        def expand_query(q):
            return ["only one"]

    monkeypatch.setattr(search_unified, "_rag_search", _FakeRagSearch)

    q, variants = search_unified._apply_bilingual_expansion("hello")
    assert q == "hello"
    assert variants == []


def test_bilingual_filters_duplicate_and_empty_alternates(monkeypatch):
    """Alternates that match the new primary query OR are empty strings
    must be filtered out before slicing to [:2]."""
    import search_unified

    monkeypatch.setattr(search_unified, "_RAG_IN_PROCESS", True)

    class _FakeRagSearch:
        @staticmethod
        def expand_query(q):
            return ["primary", "primary", "", "valid_alt", "another_alt"]

    monkeypatch.setattr(search_unified, "_rag_search", _FakeRagSearch)

    q, variants = search_unified._apply_bilingual_expansion("hello")
    assert q == "primary"
    assert variants == ["valid_alt", "another_alt"]


def test_bilingual_caps_alternates_at_two(monkeypatch):
    import search_unified

    monkeypatch.setattr(search_unified, "_RAG_IN_PROCESS", True)

    class _FakeRagSearch:
        @staticmethod
        def expand_query(q):
            return ["primary", "a", "b", "c", "d", "e"]

    monkeypatch.setattr(search_unified, "_rag_search", _FakeRagSearch)

    _q, variants = search_unified._apply_bilingual_expansion("hello")
    assert variants == ["a", "b"]


# ── _apply_ontology_expansion ────────────────────────────────────────


def _stub_maybe_expand(monkeypatch, expanded_query, terms, elapsed_ms):
    """Replace maybe_expand_query_with_ontology with a deterministic stub."""
    import search_unified

    monkeypatch.setattr(
        search_unified,
        "maybe_expand_query_with_ontology",
        lambda q: (expanded_query, terms, elapsed_ms),
    )


def test_ontology_disabled_is_noop(monkeypatch):
    """When BRAIN_ONTOLOGY_EXPANSION_ENABLED is False, helper returns
    (query unchanged, "", []) and NEVER touches source_timing."""
    import search_unified

    monkeypatch.setattr(search_unified, "BRAIN_ONTOLOGY_EXPANSION_ENABLED", False)
    # Sentinel: maybe_expand_query_with_ontology must NOT be called.
    monkeypatch.setattr(
        search_unified,
        "maybe_expand_query_with_ontology",
        lambda q: (_ for _ in ()).throw(AssertionError("should not be called")),
    )

    timing: dict = {}
    q, sidecar, terms = search_unified._apply_ontology_expansion("query", timing)
    assert q == "query"
    assert sidecar == ""
    assert terms == []
    assert timing == {}


def test_ontology_enabled_inline_mode_rewrites_query(monkeypatch):
    """Inline mode (BRAIN_ONTOLOGY_EXPANSION_MODE != 'sidecar'): the
    expanded query replaces the input query; sidecar stays empty."""
    import search_unified

    monkeypatch.setattr(search_unified, "BRAIN_ONTOLOGY_EXPANSION_ENABLED", True)
    monkeypatch.setattr(search_unified, "BRAIN_ONTOLOGY_EXPANSION_MODE", "inline")
    _stub_maybe_expand(monkeypatch, "query expanded foo bar", ["foo", "bar"], 12)

    timing: dict = {}
    q, sidecar, terms = search_unified._apply_ontology_expansion("query", timing)
    assert q == "query expanded foo bar"
    assert sidecar == ""
    assert terms == ["foo", "bar"]
    assert timing["ontology_expansion_terms"] == 2
    assert timing["ontology_expansion_applied"] is True
    assert timing["ontology_expansion_ms"] == 12
    assert timing["ontology_expansion_sidecar_mode"] == 0


def test_ontology_enabled_sidecar_mode_keeps_primary_and_writes_sidecar(monkeypatch):
    """Sidecar mode + non-empty terms: primary query stays untouched,
    expanded text lands in ontology_sidecar_query."""
    import search_unified

    monkeypatch.setattr(search_unified, "BRAIN_ONTOLOGY_EXPANSION_ENABLED", True)
    monkeypatch.setattr(search_unified, "BRAIN_ONTOLOGY_EXPANSION_MODE", "sidecar")
    _stub_maybe_expand(monkeypatch, "expanded query", ["alpha", "beta"], 5)

    timing: dict = {}
    q, sidecar, terms = search_unified._apply_ontology_expansion("orig", timing)
    assert q == "orig"  # primary unchanged
    assert sidecar == "expanded query"
    assert terms == ["alpha", "beta"]
    assert timing["ontology_expansion_sidecar_mode"] == 1


def test_ontology_sidecar_mode_no_terms_keeps_query(monkeypatch):
    """Sidecar mode but expansion produced no terms: primary stays as the
    raw input (NOT the expanded form) and sidecar stays empty."""
    import search_unified

    monkeypatch.setattr(search_unified, "BRAIN_ONTOLOGY_EXPANSION_ENABLED", True)
    monkeypatch.setattr(search_unified, "BRAIN_ONTOLOGY_EXPANSION_MODE", "sidecar")
    _stub_maybe_expand(monkeypatch, "irrelevant", [], 1)

    timing: dict = {}
    q, sidecar, terms = search_unified._apply_ontology_expansion("orig", timing)
    # When terms is empty, the `else` branch fires (query = expanded_query)
    # even in sidecar mode, because the `terms and ...` guard requires terms.
    assert q == "irrelevant"
    assert sidecar == ""
    assert terms == []


def test_ontology_inline_no_terms_still_writes_timing(monkeypatch):
    """Inline mode with empty terms still records the source_timing keys
    (terms=0, applied=False, ms set)."""
    import search_unified

    monkeypatch.setattr(search_unified, "BRAIN_ONTOLOGY_EXPANSION_ENABLED", True)
    monkeypatch.setattr(search_unified, "BRAIN_ONTOLOGY_EXPANSION_MODE", "inline")
    _stub_maybe_expand(monkeypatch, "expanded but no terms", [], 7)

    timing: dict = {}
    q, sidecar, terms = search_unified._apply_ontology_expansion("orig", timing)
    assert timing == {
        "ontology_expansion_terms": 0,
        "ontology_expansion_applied": False,
        "ontology_expansion_ms": 7,
        "ontology_expansion_sidecar_mode": 0,
    }
    # No terms → falls into else (query = expanded_query)
    assert q == "expanded but no terms"
    assert sidecar == ""
