"""Unit tests for brain_core.crag — Phase M9 iterative retrieval scaffold."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


@pytest.fixture
def crag_module():
    if "crag" in sys.modules:
        del sys.modules["crag"]
    import crag

    return crag


def _result(score: float, ce: float = 0.5, title: str = "T", content: str = "C") -> dict:
    return {"score": score, "cross_encoder_score": ce, "title": title, "content": content}


def test_score_confidence_empty_results(crag_module):
    report = crag_module.score_confidence([])
    assert report.score == 0.0
    assert report.n_results == 0


def test_score_confidence_high_quality(crag_module):
    """Mimic the production high-quality query: top ~110, ce ~0.70, wide spread."""
    results = [
        _result(110.5, ce=0.70),
        _result(83.1, ce=0.72),
        _result(82.0, ce=0.66),
        _result(71.2, ce=0.68),
        _result(70.1, ce=0.68),
    ]
    report = crag_module.score_confidence(results)
    assert report.score >= 0.7, f"high-quality should score >= 0.7, got {report.score}"
    assert report.top_score == 110.5
    assert report.score_spread == pytest.approx(40.4, abs=0.1)
    assert report.ce_signal_present is True
    assert crag_module.should_iterate(report) is False


def test_score_confidence_low_quality_gibberish(crag_module):
    """Mimic the gibberish query: top ~53, ce all 0.5 (median fill), narrow spread."""
    results = [
        _result(53.1, ce=0.5),
        _result(51.0, ce=0.5),
        _result(50.8, ce=0.5),
        _result(50.7, ce=0.5),
        _result(50.5, ce=0.5),
    ]
    report = crag_module.score_confidence(results)
    assert report.score < 0.4, f"gibberish should score < 0.4, got {report.score}"
    assert report.ce_signal_present is False
    assert report.score_spread < crag_module.LOW_CONFIDENCE_SPREAD
    assert crag_module.should_iterate(report) is True


def test_should_iterate_empty_returns_false(crag_module):
    """Empty result sets shouldn't trigger expansion — there's nothing to expand from."""
    report = crag_module.score_confidence([])
    assert crag_module.should_iterate(report) is False


def test_expand_query_uses_dispatch_fn(crag_module):
    """expand_query should call the injected dispatch_fn with a prompt that
    includes the original query and the top-3 results."""
    captured: list[tuple[str, str]] = []

    def fake_dispatch(agent: str, prompt: str) -> str:
        captured.append((agent, prompt))
        return "rewritten query string"

    results = [_result(50, title="Hit1", content="content1"), _result(45, title="Hit2", content="content2")]
    rewritten = crag_module.expand_query("original q", results, dispatch_fn=fake_dispatch)
    assert rewritten == "rewritten query string"
    assert len(captured) == 1
    assert captured[0][0] == "jenna"
    assert "original q" in captured[0][1]
    assert "Hit1" in captured[0][1]


def test_expand_query_returns_none_on_dispatch_failure(crag_module):
    def fake_dispatch(agent: str, prompt: str) -> str:
        raise RuntimeError("dispatch broke")

    rewritten = crag_module.expand_query("q", [_result(50)], dispatch_fn=fake_dispatch)
    assert rewritten is None


def test_expand_query_returns_none_when_rewrite_equals_original(crag_module):
    def fake_dispatch(agent: str, prompt: str) -> str:
        return "  q  "  # trimmed → equals original

    rewritten = crag_module.expand_query("q", [_result(50)], dispatch_fn=fake_dispatch)
    assert rewritten is None


def test_iterative_recall_high_confidence_no_iteration(crag_module):
    """High-confidence first-hop results → CRAG returns immediately."""
    call_count = {"n": 0}

    def fake_recall(q: str) -> list[dict]:
        call_count["n"] += 1
        return [_result(110, ce=0.7), _result(80, ce=0.7), _result(70, ce=0.7)]

    def fake_expand(q: str, results: list[dict]) -> str:
        return "should not be called"

    results, telemetry = crag_module.iterative_recall(
        "good query", fake_recall, max_hops=2, expand_fn=fake_expand
    )
    assert call_count["n"] == 1
    assert telemetry["hops"] == 1
    assert telemetry["iterated"] is False
    assert len(results) == 3


def test_iterative_recall_low_confidence_triggers_expansion(crag_module):
    """Low-confidence first hop → expand → second hop. Telemetry reflects both."""
    call_count = {"n": 0}

    def fake_recall(q: str) -> list[dict]:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return [_result(50, ce=0.5), _result(48, ce=0.5)]
        return [_result(105, ce=0.7), _result(85, ce=0.7), _result(70, ce=0.7)]

    def fake_expand(q: str, results: list[dict]) -> str:
        return f"{q} better"

    results, telemetry = crag_module.iterative_recall(
        "weak query", fake_recall, max_hops=2, expand_fn=fake_expand
    )
    assert call_count["n"] == 2
    assert telemetry["hops"] == 2
    assert telemetry["iterated"] is True
    assert telemetry["expansions"] == ["weak query better"]
    # Best results should be the second hop (higher confidence)
    assert results[0]["score"] == 105


def test_iterative_recall_returns_best_when_expansion_fails(crag_module):
    """If expansion returns None, return the best results seen so far."""
    call_count = {"n": 0}

    def fake_recall(q: str) -> list[dict]:
        call_count["n"] += 1
        return [_result(50, ce=0.5)]

    def fake_expand(q: str, results: list[dict]) -> None:
        return None  # expansion failed

    results, telemetry = crag_module.iterative_recall(
        "weak query", fake_recall, max_hops=2, expand_fn=fake_expand
    )
    assert call_count["n"] == 1
    assert telemetry["hops"] == 1
    assert telemetry["iterated"] is False
    assert len(results) == 1
