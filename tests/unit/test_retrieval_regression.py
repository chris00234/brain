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
