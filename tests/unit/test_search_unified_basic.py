"""Smoke tests for brain_core/search_unified.py — until now it had
zero test coverage despite being the hottest path in the codebase.

These are intentionally minimal: import safety, deduplicate correctness,
trace logging side-effect. The real relevance testing lives in the
eval_holdout/stable/extended suites.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core"))


def test_module_imports_without_raising():
    import search_unified  # noqa: F401


def test_deduplicate_drops_exact_duplicates():
    from search_unified import deduplicate

    results = [
        {"path": "a.md", "score": 100, "content": "foo"},
        {"path": "a.md", "score": 80, "content": "foo"},  # duplicate path
        {"path": "b.md", "score": 90, "content": "bar"},
    ]
    out = deduplicate(results)
    # Unique by path — kept higher-score version
    assert len(out) == 2
    paths = [r["path"] for r in out]
    assert paths.count("a.md") == 1
    # Should keep the higher score
    a = next(r for r in out if r["path"] == "a.md")
    assert a["score"] == 100


def test_search_trace_writes_when_enabled(tmp_path, monkeypatch):
    """Verify trace log emits a JSONL entry."""
    import search_unified

    trace_path = tmp_path / "search_trace.jsonl"
    monkeypatch.setattr(search_unified, "_SEARCH_TRACE_PATH", trace_path)

    search_unified._maybe_emit_search_trace(
        query="test query",
        original_query=None,
        source_counts={"rag": 3, "canonical": 1},
        source_timing={"rag": 120, "canonical": 45},
        trust_weights=[0.9, 1.0],
        fusion_mode="rrf",
        total_after_fusion=4,
        intent_boost={"rag": 1.0, "canonical": 1.0},
    )

    assert trace_path.exists()
    lines = trace_path.read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["q"] == "test query"
    assert rec["src_counts"]["rag"] == 3
    assert rec["fusion"] == "rrf"


def test_search_trace_disabled_via_env(tmp_path, monkeypatch):
    import search_unified

    trace_path = tmp_path / "search_trace.jsonl"
    monkeypatch.setattr(search_unified, "_SEARCH_TRACE_PATH", trace_path)
    monkeypatch.setenv("BRAIN_SEARCH_TRACE_DISABLED", "1")

    search_unified._maybe_emit_search_trace(
        query="test",
        original_query=None,
        source_counts={},
        source_timing={},
        trust_weights=[],
        fusion_mode="rrf",
        total_after_fusion=0,
        intent_boost={},
    )

    assert not trace_path.exists()


def test_primary_doc_hits_route_korean_name_queries_to_identity():
    import search_unified

    hits = search_unified._primary_doc_hits("What is Chris's Korean name?")

    paths = {hit["path"] for hit in hits}
    assert "/Users/chrischo/server/knowledge/canonical/chris/_identity.md" in paths
    identity = next(hit for hit in hits if hit["path"].endswith("/_identity.md"))
    assert "조대현" in identity["content"]
    assert "Daehyun Cho" in identity["content"]
    assert identity["metadata"]["canonical_lookup"] is True


def test_primary_doc_hits_route_hangul_name_queries_to_identity():
    import search_unified

    hits = search_unified._primary_doc_hits("조대현")

    assert any(hit["path"].endswith("/_identity.md") for hit in hits)


def test_primary_doc_hits_do_not_overroute_family_name_queries_to_chris_identity():
    import search_unified

    hits = search_unified._primary_doc_hits("What is Chris's wife's Korean name?")

    assert not any(hit["path"].endswith("/_identity.md") for hit in hits)


def test_relationship_name_queries_suppress_chris_identity_result():
    import search_unified

    assert search_unified._should_suppress_chris_identity_for_query("What is Chris's wife's Korean name?")
    assert not search_unified._should_suppress_chris_identity_for_query("What is Chris's Korean name?")
    assert not search_unified._should_suppress_chris_identity_for_query("조대현")


def test_normalize_rag_result_uses_existing_document_id_as_source_document_id():
    import search_unified

    result = search_unified.normalize_rag_result(
        {
            "id": "chunk-1",
            "collection": "knowledge",
            "source": "/tmp/source.md",
            "content": "This content is long enough to look like a real indexed chunk.",
            "type": "manual-note",
            "section": "Intro",
            "metadata": {"document_id": "doc:source:abc", "source_path": "/tmp/source.md"},
        }
    )

    assert result["metadata"]["document_id"] == "doc:source:abc"
    assert result["metadata"]["source_document_id"] == "doc:source:abc"


# ── Phase 0: freshness-aware trust decay ────────────────────────


def test_effective_trust_fresh_canonical_keeps_full_weight(monkeypatch):
    """Today's canonical write should still get the full 1.0 trust ceiling."""
    import search_unified

    monkeypatch.setenv("BRAIN_TRUST_FRESHNESS", "on")
    now = 1_745_000_000.0  # arbitrary anchor
    written = now - 60  # 1 minute old
    trust = search_unified._effective_trust("canonical", written, _now_ts=now)
    # Continuous decay: 60s ≈ 0.0007 days x 0.02/day ≈ 1.4e-5 below 1.0
    assert 0.9999 <= trust <= 1.0


def test_effective_trust_old_canonical_decays_to_floor(monkeypatch):
    """A canonical note older than ~10 days should fall to the semantic_memory
    floor (0.8) so a fresh atom can win the ranking fight.
    """
    import search_unified

    monkeypatch.setenv("BRAIN_TRUST_FRESHNESS", "on")
    now = 1_745_000_000.0
    # 30 days old: 1.0 - 30 * 0.02 = 0.4 -> floored at 0.8
    written = now - (30 * 86400)
    trust = search_unified._effective_trust("canonical", written, _now_ts=now)
    assert trust == search_unified._TRUST_FLOOR  # 0.8


def test_effective_trust_disabled_via_env(monkeypatch):
    """Setting BRAIN_TRUST_FRESHNESS=off should restore the static dict."""
    import search_unified

    monkeypatch.setenv("BRAIN_TRUST_FRESHNESS", "off")
    now = 1_745_000_000.0
    very_old = now - (1000 * 86400)
    trust = search_unified._effective_trust("canonical", very_old, _now_ts=now)
    assert trust == 1.0  # static SOURCE_TRUST["canonical"]


def test_effective_trust_handles_iso_strings(monkeypatch):
    """ISO timestamp strings (from Qdrant payloads) decay correctly."""
    from datetime import UTC, datetime

    import search_unified

    monkeypatch.setenv("BRAIN_TRUST_FRESHNESS", "on")
    # 5 days ago in ISO form
    now = datetime(2026, 4, 27, 16, 0, 0, tzinfo=UTC).timestamp()
    written_iso = "2026-04-22T16:00:00Z"
    # 1.0 - 5 * 0.02 = 0.9; above floor
    trust = search_unified._effective_trust("canonical", written_iso, _now_ts=now)
    assert abs(trust - 0.9) < 0.001


def test_effective_trust_handles_offset_aware_iso_strings(monkeypatch):
    from datetime import UTC, datetime

    import search_unified

    monkeypatch.setenv("BRAIN_TRUST_FRESHNESS", "on")
    now = datetime(2026, 4, 27, 16, 0, 0, tzinfo=UTC).timestamp()
    written_iso = "2026-04-23T01:00:00+09:00"  # same instant as 2026-04-22T16:00:00Z

    trust = search_unified._effective_trust("canonical", written_iso, _now_ts=now)

    assert abs(trust - 0.9) < 0.001


def test_effective_trust_no_timestamp_returns_base(monkeypatch):
    """If we have no timestamp we cannot decay — fall back to static trust
    rather than guessing.
    """
    import search_unified

    monkeypatch.setenv("BRAIN_TRUST_FRESHNESS", "on")
    assert search_unified._effective_trust("canonical", None) == 1.0
    assert search_unified._effective_trust("canonical", "") == 1.0
    assert search_unified._effective_trust("knowledge", None) == 0.9


def test_effective_trust_unaffected_sources_keep_static_weight(monkeypatch):
    """semantic_memory, personal, etc. have no decay configured — they always
    return the static value regardless of age.
    """
    import search_unified

    monkeypatch.setenv("BRAIN_TRUST_FRESHNESS", "on")
    now = 1_745_000_000.0
    very_old = now - (365 * 86400)
    assert search_unified._effective_trust("semantic_memory", very_old, _now_ts=now) == 0.8
    assert search_unified._effective_trust("personal", very_old, _now_ts=now) == 0.85
    assert search_unified._effective_trust("graph", very_old, _now_ts=now) == 0.5
