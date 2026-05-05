from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "crag_correction_regression", ROOT / "cli" / "crag_correction_regression.py"
)
crag_correction = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(crag_correction)


def test_crag_correction_recovers_with_deterministic_rewrite(tmp_path, monkeypatch):
    eval_set = tmp_path / "eval.json"
    eval_set.write_text(
        json.dumps(
            [
                {
                    "query": "weak q",
                    "collection": "all",
                    "expected_content": "needle",
                    "correction_queries": ["better q"],
                }
            ]
        )
    )
    calls: list[dict] = []
    fake = ModuleType("search_unified")

    def search_all(query, **kwargs):
        calls.append({"query": query, **kwargs})
        if query == "better q":
            return {
                "results": [{"title": "hit", "content": "needle", "score": 110, "cross_encoder_score": 0.8}]
            }
        return {"results": []}

    fake.search_all = search_all
    monkeypatch.setitem(sys.modules, "search_unified", fake)
    monkeypatch.setattr(crag_correction, "REPORT_FILE", tmp_path / "report.json")
    monkeypatch.setenv("BRAIN_CRAG_CORRECTION_MIN_CASES", "1")

    out = crag_correction.run(eval_set, top_k=3)

    assert out["status"] == "ok"
    assert out["recovery_needed"] == 1
    assert out["recovered"] == 1
    assert calls[0] == {"query": "weak q", "limit": 3}
    assert calls[1] == {"query": "better q", "limit": 3}


def test_crag_correction_blocks_insufficient_coverage(tmp_path, monkeypatch):
    eval_set = tmp_path / "eval.json"
    eval_set.write_text(json.dumps([{"query": "already good", "expected_content": "needle"}]))
    fake = ModuleType("search_unified")
    fake.search_all = lambda query, **kwargs: {"results": [{"content": "needle", "score": 100}]}
    monkeypatch.setitem(sys.modules, "search_unified", fake)
    monkeypatch.setattr(crag_correction, "REPORT_FILE", tmp_path / "report.json")
    monkeypatch.setenv("BRAIN_CRAG_CORRECTION_MIN_CASES", "1")

    out = crag_correction.run(eval_set, top_k=3)

    assert out["status"] == "insufficient_coverage"
    assert out["recovery_needed"] == 0


def test_crag_correction_blocks_failed_recovery(tmp_path, monkeypatch):
    eval_set = tmp_path / "eval.json"
    eval_set.write_text(
        json.dumps(
            [
                {"query": "q1", "expected_content": "needle", "correction_queries": ["q1 better"]},
                {"query": "q2", "expected_content": "needle", "correction_queries": ["q2 better"]},
            ]
        )
    )
    fake = ModuleType("search_unified")
    fake.search_all = lambda query, **kwargs: {"results": []}
    monkeypatch.setitem(sys.modules, "search_unified", fake)
    monkeypatch.setattr(crag_correction, "REPORT_FILE", tmp_path / "report.json")
    monkeypatch.setenv("BRAIN_CRAG_CORRECTION_MIN_CASES", "2")

    out = crag_correction.run(eval_set, top_k=3)

    assert out["status"] == "breached"
    assert out["failed_recoveries"] == 2


def test_crag_correction_llm_rewrite_uses_separate_report(tmp_path, monkeypatch):
    eval_set = tmp_path / "eval.json"
    eval_set.write_text(
        json.dumps(
            [
                {
                    "query": "weak q",
                    "collection": "all",
                    "expected_content": "needle",
                    "correction_queries": ["deterministic should not be used"],
                }
            ]
        )
    )
    fake = ModuleType("search_unified")

    def search_all(query, **kwargs):
        if query == "llm better q":
            return {
                "results": [{"title": "hit", "content": "needle", "score": 110, "cross_encoder_score": 0.8}]
            }
        return {"results": []}

    fake.search_all = search_all
    monkeypatch.setitem(sys.modules, "search_unified", fake)
    monkeypatch.setattr(crag_correction, "REPORT_FILE", tmp_path / "deterministic.json")
    monkeypatch.setattr(crag_correction, "LLM_REPORT_FILE", tmp_path / "llm.json")
    monkeypatch.setattr(
        crag_correction,
        "_live_rewrite_candidates",
        lambda query, weak_results, timeout_s: [{"source": "llm", "query": "llm better q"}],
    )
    monkeypatch.setenv("BRAIN_CRAG_CORRECTION_MIN_CASES", "1")

    out = crag_correction.run(eval_set, top_k=3, rewrite_source="llm")

    assert out["status"] == "ok"
    assert out["rewrite_source"] == "llm"
    assert out["recovered"] == 1
    assert (tmp_path / "llm.json").exists()
    assert not (tmp_path / "deterministic.json").exists()
    assert out["rows"][0]["correction_attempts"][0]["source"] == "llm"


def test_crag_correction_live_rewrite_tries_rule_candidate_before_failed_llm(tmp_path, monkeypatch):
    eval_set = tmp_path / "eval.json"
    eval_set.write_text(
        json.dumps(
            [
                {
                    "query": "calendar dinner",
                    "collection": "calendar",
                    "expected_content": "저녁",
                }
            ]
        )
    )
    fake = ModuleType("search_unified")

    def search_all(query, **kwargs):
        if query == "저녁 약속":
            return {
                "results": [{"title": "hit", "content": "저녁", "score": 110, "cross_encoder_score": 0.8}]
            }
        return {"results": []}

    fake.search_all = search_all
    monkeypatch.setitem(sys.modules, "search_unified", fake)
    monkeypatch.setattr(crag_correction, "REPORT_FILE", tmp_path / "deterministic.json")
    monkeypatch.setattr(crag_correction, "LLM_REPORT_FILE", tmp_path / "llm.json")
    monkeypatch.setattr(
        crag_correction,
        "_live_rewrite_candidates",
        lambda query, weak_results, timeout_s: [
            {"source": "rule", "query": "저녁 약속"},
            {"source": "llm", "query": "bad generic dinner query"},
        ],
    )
    monkeypatch.setenv("BRAIN_CRAG_CORRECTION_MIN_CASES", "1")

    out = crag_correction.run(eval_set, top_k=3, rewrite_source="llm")

    assert out["status"] == "ok"
    assert out["recovered"] == 1
    assert out["rows"][0]["best_query"] == "저녁 약속"
    assert out["rows"][0]["correction_attempts"][0]["source"] == "rule"
