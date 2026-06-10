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
        return [
            _result(110, ce=0.7, content="good query answer"),
            _result(80, ce=0.7, content="good query support"),
            _result(70, ce=0.7, content="good query context"),
        ]

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
        return [
            _result(105, ce=0.7, content="weak query better answer"),
            _result(85, ce=0.7, content="weak query better support"),
            _result(70, ce=0.7, content="weak query better context"),
        ]

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


def test_query_coverage_penalizes_unrelated_confident_window(crag_module):
    results = [
        _result(110, ce=0.8, title="deployment notes", content="service healthcheck only"),
        _result(90, ce=0.75, title="infra", content="uptime kuma"),
        _result(80, ce=0.72, title="ops", content="docker compose"),
    ]
    report = crag_module.score_confidence(results, query="agent named bartholomew")
    assert report.components["query_coverage"] < 0.34
    assert crag_module.should_iterate(report) is True


def test_expand_query_uses_cli_default_chain_unless_backend_env(crag_module, monkeypatch):
    captured: list[dict] = []

    class Result:
        ok = True
        text = "rewritten"

    def fake_dispatch(agent: str, prompt: str, **kwargs):
        captured.append({"agent": agent, **kwargs})
        return Result()

    import types

    fake_cli_llm = types.ModuleType("cli_llm")
    fake_cli_llm.dispatch = fake_dispatch
    monkeypatch.setitem(sys.modules, "cli_llm", fake_cli_llm)
    monkeypatch.delenv("BRAIN_CRAG_EXPAND_BACKEND", raising=False)

    rewritten = crag_module.expand_query("original", [_result(50, content="weak")])

    assert rewritten == "rewritten"
    assert captured[0]["backend"] is None
    assert "max_backends" not in captured[0]

    captured.clear()
    monkeypatch.setenv("BRAIN_CRAG_EXPAND_BACKEND", "openclaw")

    rewritten = crag_module.expand_query("original", [_result(50, content="weak")])

    assert rewritten == "rewritten"
    assert captured[0]["backend"] == "openclaw"
    assert "max_backends" not in captured[0]


def test_rule_based_rewrite_candidates_bridge_personal_source_terms(crag_module):
    assert crag_module.rule_based_rewrite_candidates("영주권 갱신")[0] == "USCIS I-751 receipt notice"
    assert "저녁 약속" in crag_module.rule_based_rewrite_candidates("dinner appointment 저녁 약속")
    assert crag_module.rule_based_rewrite_candidates("renewal receipt")[0] == "receipt notice"


def test_expand_query_uses_rule_candidate_before_llm(crag_module):
    called = False

    def fake_dispatch(agent: str, prompt: str) -> str:
        nonlocal called
        called = True
        return "llm query"

    rewritten = crag_module.expand_query(
        "calendar dinner",
        [],
        dispatch_fn=fake_dispatch,
    )

    assert rewritten == "저녁 약속"
    assert called is False


# ── Data-driven rewrite bridges (crag_rewrites.yaml) ─────────────────────
# The bridge VOCABULARY is data, not code: the loader is generic and must
# fail-open. Fixture bridges use invented terms so these tests pin the
# mechanism, never Chris-specific corpus vocabulary.


def test_load_rewrite_bridges_from_fixture_yaml(crag_module, tmp_path):
    fixture = tmp_path / "bridges.yaml"
    fixture.write_text(
        "version: 1\n"
        "bridges:\n"
        "  - when_terms: [보고서, 분기]\n"
        "    rewrites: [quarterly report fixture, fixture report archive]\n"
        "  - when_terms: [widget]\n"
        "    rewrites: [widget assembly manual]\n"
    )
    bridges = crag_module._load_rewrite_bridges(fixture)
    assert bridges == (
        (("보고서", "분기"), ("quarterly report fixture", "fixture report archive")),
        (("widget",), ("widget assembly manual",)),
    )


def test_rewrite_candidates_use_fixture_bridges(crag_module, tmp_path, monkeypatch):
    fixture = tmp_path / "bridges.yaml"
    fixture.write_text("bridges:\n  - when_terms: [보고서]\n    rewrites: [quarterly report fixture]\n")
    monkeypatch.setattr(crag_module, "_REWRITE_BRIDGES_PATH", fixture)
    monkeypatch.setattr(crag_module, "_bridges_cache", None)
    monkeypatch.setattr(crag_module, "_bridges_mtime", -1.0)
    # KO paraphrase carrying the bridge term still fires (substring match).
    assert "quarterly report fixture" in crag_module.rule_based_rewrite_candidates("지난 분기 보고서 찾아줘")
    # Negative control: unrelated query gets no bridge rewrites.
    assert crag_module.rule_based_rewrite_candidates("unrelated infra question") == []


def test_load_rewrite_bridges_fail_open_missing_and_malformed(crag_module, tmp_path):
    assert crag_module._load_rewrite_bridges(tmp_path / "missing.yaml") == ()
    bad = tmp_path / "bad.yaml"
    bad.write_text("bridges:\n  - when_terms: [\n")  # malformed YAML
    assert crag_module._load_rewrite_bridges(bad) == ()
    # Entries missing either side are skipped, not fatal.
    partial = tmp_path / "partial.yaml"
    partial.write_text(
        "bridges:\n"
        "  - when_terms: [orphan]\n"
        "  - rewrites: [no trigger]\n"
        "  - when_terms: [ok]\n    rewrites: [ok rewrite]\n"
    )
    assert crag_module._load_rewrite_bridges(partial) == ((("ok",), ("ok rewrite",)),)
