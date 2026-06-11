from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "retrieval_regression", ROOT / "cli" / "retrieval_regression.py"
)
retrieval_regression = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(retrieval_regression)


def test_retrieval_regression_scores_expected_content(tmp_path, monkeypatch):
    eval_set = tmp_path / "eval.json"
    eval_set.write_text(
        json.dumps([{"query": "q", "expected_content": "needle", "expected_source": "canonical"}])
    )
    fake = ModuleType("search_unified")
    fake.search_all = lambda query, **kwargs: [
        {"title": "hit", "content": "has needle", "source_type": "canonical"}
    ]
    monkeypatch.setitem(sys.modules, "search_unified", fake)
    monkeypatch.setattr(retrieval_regression, "REPORT_FILE", tmp_path / "report.json")

    out = retrieval_regression.run(eval_set, limit=1, top_k=3)

    assert out["status"] == "ok"
    assert out["passed"] == 1
    assert out["min_pass_rate"] == 80.0
    assert (tmp_path / "report.json").exists()


def test_retrieval_regression_honors_min_pass_rate_env(tmp_path, monkeypatch):
    eval_set = tmp_path / "eval.json"
    eval_set.write_text(json.dumps([{"query": "q", "expected_content": "needle"}]))
    fake = ModuleType("search_unified")
    fake.search_all = lambda query, **kwargs: [{"content": "miss"}]
    monkeypatch.setitem(sys.modules, "search_unified", fake)
    monkeypatch.setattr(retrieval_regression, "REPORT_FILE", tmp_path / "report.json")
    monkeypatch.setenv("BRAIN_RETRIEVAL_REGRESSION_MIN_PASS_RATE", "1")

    out = retrieval_regression.run(eval_set, limit=1, top_k=3)

    assert out["status"] == "breached"
    assert out["min_pass_rate"] == 1.0


def test_retrieval_regression_treats_all_collection_as_unscoped(tmp_path, monkeypatch):
    eval_set = tmp_path / "eval.json"
    eval_set.write_text(json.dumps([{"query": "q", "collection": "all", "expected_content": "needle"}]))
    calls = []
    fake = ModuleType("search_unified")

    def search_all(query, **kwargs):
        calls.append(kwargs)
        return [{"content": "needle"}]

    fake.search_all = search_all  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "search_unified", fake)
    monkeypatch.setattr(retrieval_regression, "REPORT_FILE", tmp_path / "report.json")

    out = retrieval_regression.run(eval_set, limit=1, top_k=3)

    assert out["status"] == "ok"
    assert calls == [{"limit": 3}]


def test_retrieval_regression_honors_alternates_and_forbidden_content(tmp_path, monkeypatch):
    eval_set = tmp_path / "eval.json"
    eval_set.write_text(
        json.dumps(
            [
                {"query": "alternate", "expected_content": "needle", "expected_alternates": ["fallback"]},
                {"query": "forbidden", "expected_content": "needle", "forbidden_content": ["leak"]},
            ]
        )
    )
    fake = ModuleType("search_unified")

    def search_all(query, **kwargs):
        if query == "alternate":
            return [{"content": "fallback"}]
        return [{"content": "needle plus leak"}]

    fake.search_all = search_all  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "search_unified", fake)
    monkeypatch.setattr(retrieval_regression, "REPORT_FILE", tmp_path / "report.json")

    out = retrieval_regression.run(eval_set, limit=2, top_k=3)

    assert out["passed"] == 1
    assert out["rows"][0]["alternate_hit"] is True
    assert out["rows"][1]["forbidden_hit"] is True
    assert out["rows"][1]["ok"] is False


def test_default_eval_selection_includes_quality_gate_categories_beyond_limit(tmp_path, monkeypatch):
    eval_set = tmp_path / "eval_set_stable.json"
    eval_set.write_text(
        json.dumps(
            [
                {"query": "baseline-1"},
                {"query": "baseline-2"},
                {"query": "later-stale", "category": "stale_fact_supersession"},
                {"query": "later-noise", "category": "clean_hit_topk_noise"},
                {"query": "later-other", "category": "non_gate_category"},
            ]
        )
    )
    monkeypatch.setattr(retrieval_regression, "DEFAULT_EVAL_SET", eval_set)

    cases = retrieval_regression._load_cases(eval_set, limit=2)

    assert [case["query"] for case in cases] == ["baseline-1", "baseline-2", "later-stale", "later-noise"]


def test_search_unified_route_guarantee_hit_shape():
    import search_unified

    hits = search_unified._route_guarantee_hits(
        "Brain recall quality noise prefetch empty summary Claude Code session canonical_first current useful context eval score"
    )

    assert hits
    assert hits[0]["source_type"] == "route_guarantee"
    assert hits[0]["path"] == "route_guarantee:brain_recall_prefetch_quality_eval_no_noise"
    assert "maximize useful current memory context" in hits[0]["content"]


def test_search_unified_identity_location_query_maps_to_identity_primary_doc():
    import search_unified

    hits = search_unified._primary_doc_hits("Where is Chris based and what timezone does he work in?")

    assert any(str(hit.get("path", "")).endswith("canonical/chris/_identity.md") for hit in hits)
