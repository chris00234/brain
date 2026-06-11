from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("crag_regression", ROOT / "cli" / "crag_regression.py")
crag_regression = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(crag_regression)


def test_crag_regression_flags_dangerous_false_accept(tmp_path, monkeypatch):
    eval_set = tmp_path / "eval.json"
    eval_set.write_text(json.dumps([{"query": "q", "expected_content": "needle"}]))
    fake = ModuleType("search_unified")
    fake.search_all = lambda query, **kwargs: [
        {"title": "confident miss", "content": "wrong", "score": 110, "cross_encoder_score": 0.8},
        {"title": "also wrong", "content": "wrong", "score": 80, "cross_encoder_score": 0.75},
        {"title": "third", "content": "wrong", "score": 70, "cross_encoder_score": 0.72},
    ]
    monkeypatch.setitem(sys.modules, "search_unified", fake)
    monkeypatch.setattr(crag_regression, "REPORT_FILE", tmp_path / "report.json")
    monkeypatch.setenv("BRAIN_CRAG_MAX_CORRECTIVE_TRIGGER_RATE", "100")

    out = crag_regression.run(eval_set, limit=1, top_k=3)

    assert out["status"] == "breached"
    assert out["dangerous_false_accepts"] == 1
    assert out["safety_rate"] == 0.0


def test_crag_regression_allows_low_confidence_correction(tmp_path, monkeypatch):
    eval_set = tmp_path / "eval.json"
    eval_set.write_text(json.dumps([{"query": "q", "expected_content": "needle"}]))
    fake = ModuleType("search_unified")
    fake.search_all = lambda query, **kwargs: [
        {"title": "weak miss", "content": "wrong", "score": 53, "cross_encoder_score": 0.5},
        {"title": "weak miss 2", "content": "wrong", "score": 51, "cross_encoder_score": 0.5},
        {"title": "weak miss 3", "content": "wrong", "score": 50, "cross_encoder_score": 0.5},
    ]
    monkeypatch.setitem(sys.modules, "search_unified", fake)
    monkeypatch.setattr(crag_regression, "REPORT_FILE", tmp_path / "report.json")
    monkeypatch.setenv("BRAIN_CRAG_MAX_CORRECTIVE_TRIGGER_RATE", "100")

    out = crag_regression.run(eval_set, limit=1, top_k=3)

    assert out["status"] == "ok"
    assert out["dangerous_false_accepts"] == 0
    assert out["corrective_candidates"] == 1


def test_crag_regression_treats_all_collection_as_unscoped(tmp_path, monkeypatch):
    eval_set = tmp_path / "eval.json"
    eval_set.write_text(json.dumps([{"query": "q", "collection": "all", "expected_content": "needle"}]))
    calls = []
    fake = ModuleType("search_unified")

    def search_all(query, **kwargs):
        calls.append(kwargs)
        return [{"title": "hit", "content": "needle", "score": 110, "cross_encoder_score": 0.8}]

    fake.search_all = search_all
    monkeypatch.setitem(sys.modules, "search_unified", fake)
    monkeypatch.setattr(crag_regression, "REPORT_FILE", tmp_path / "report.json")

    out = crag_regression.run(eval_set, limit=1, top_k=3)

    assert out["status"] == "ok"
    assert calls == [{"limit": 3}]


def test_crag_confidence_corrects_high_score_window_missing_specific_query_term():
    from crag import score_confidence, should_iterate  # type: ignore[import-not-found]

    report = score_confidence(
        [
            {"content": "auto update schedule cron", "score": 108},
            {"content": "schedule update job", "score": 106},
            {"content": "automatic update cadence", "score": 105},
        ],
        query="watchtower auto update schedule",
    )

    assert report.components["query_coverage"] == 0.75
    assert should_iterate(report) is True
