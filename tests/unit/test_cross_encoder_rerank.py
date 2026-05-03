from __future__ import annotations

import importlib
import sys


def _reload(monkeypatch, *, adaptive: bool = True, top_k: str | None = None, simple_top_k: str | None = None):
    if adaptive:
        monkeypatch.setenv("BRAIN_CROSS_ENCODER_ADAPTIVE", "true")
    else:
        monkeypatch.delenv("BRAIN_CROSS_ENCODER_ADAPTIVE", raising=False)
    if top_k is None:
        monkeypatch.delenv("BRAIN_CROSS_ENCODER_TOP_K", raising=False)
    else:
        monkeypatch.setenv("BRAIN_CROSS_ENCODER_TOP_K", top_k)
    if simple_top_k is None:
        monkeypatch.delenv("BRAIN_CROSS_ENCODER_SIMPLE_TOP_K", raising=False)
    else:
        monkeypatch.setenv("BRAIN_CROSS_ENCODER_SIMPLE_TOP_K", simple_top_k)

    import brain_core.cross_encoder_rerank as cross_encoder_rerank

    return importlib.reload(cross_encoder_rerank)


def _results(n: int = 20):
    return [
        {"score": 120 - i, "source_type": "canonical" if i == 0 else "rag", "content": f"doc {i}"}
        for i in range(n)
    ]


def test_choose_cross_encoder_top_k_uses_full_window_when_adaptive_disabled(monkeypatch):
    cross_encoder_rerank = _reload(monkeypatch, adaptive=False, top_k="12")

    assert cross_encoder_rerank.choose_cross_encoder_top_k("simple", _results(), default_top_k=14) == 12


def test_choose_cross_encoder_top_k_uses_simple_window_for_simple_queries(monkeypatch):
    cross_encoder_rerank = _reload(monkeypatch, adaptive=True, top_k="14", simple_top_k="8")

    assert (
        cross_encoder_rerank.choose_cross_encoder_top_k("Chris Korean name", _results(), default_top_k=14)
        == 8
    )


def test_choose_cross_encoder_top_k_keeps_full_window_for_broad_queries(monkeypatch):
    cross_encoder_rerank = _reload(monkeypatch, adaptive=True, top_k="14", simple_top_k="8")

    assert (
        cross_encoder_rerank.choose_cross_encoder_top_k(
            "compare brain system and OpenClaw", _results(), default_top_k=14
        )
        == 14
    )


def test_choose_cross_encoder_top_k_caps_to_result_count(monkeypatch):
    cross_encoder_rerank = _reload(monkeypatch, adaptive=True, top_k="14", simple_top_k="8")

    assert cross_encoder_rerank.choose_cross_encoder_top_k("simple", _results(5), default_top_k=14) == 5


def test_worker_mode_uses_remote_scores_without_importing_model(monkeypatch):
    cross_encoder_rerank = _reload(monkeypatch)
    monkeypatch.setenv("BRAIN_RERANKER_MODE", "worker")
    monkeypatch.setattr(cross_encoder_rerank, "BRAIN_CROSS_ENCODER_ENABLED", True)
    sys.modules.pop("brain_core.cross_encoder_model", None)
    sys.modules.pop("cross_encoder_model", None)

    import brain_core.reranker_client as reranker_client

    monkeypatch.setattr(reranker_client, "score_pairs_remote", lambda query, docs: [0.1, 0.9])
    results = [
        {"score": 90, "source_type": "rag", "title": "first", "content": "weak"},
        {"score": 80, "source_type": "rag", "title": "second", "content": "strong"},
    ]

    reranked = cross_encoder_rerank.rerank_with_cross_encoder("query", results, top_k=2)

    assert reranked[0]["title"] == "second"
    assert "brain_core.cross_encoder_model" not in sys.modules


def test_worker_mode_failure_keeps_stage_one_results_and_does_not_import_model(monkeypatch):
    cross_encoder_rerank = _reload(monkeypatch)
    monkeypatch.setenv("BRAIN_RERANKER_MODE", "worker")
    monkeypatch.setattr(cross_encoder_rerank, "BRAIN_CROSS_ENCODER_ENABLED", True)
    sys.modules.pop("brain_core.cross_encoder_model", None)
    sys.modules.pop("cross_encoder_model", None)

    import brain_core.reranker_client as reranker_client

    def fail(query, docs):
        raise RuntimeError("worker down")

    monkeypatch.setattr(reranker_client, "score_pairs_remote", fail)
    results = [
        {"score": 90, "source_type": "rag", "title": "first", "content": "weak"},
        {"score": 80, "source_type": "rag", "title": "second", "content": "strong"},
    ]

    assert cross_encoder_rerank.rerank_with_cross_encoder("query", results, top_k=2) == results
    assert "brain_core.cross_encoder_model" not in sys.modules
