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
