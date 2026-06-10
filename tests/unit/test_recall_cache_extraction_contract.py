"""Compatibility tests for recall cache/model extraction.

The route module used to own these schemas and caches. These tests pin the
extraction seam so future route slimming does not break legacy imports or the
route-level embedding monkeypatch contract.
"""

from __future__ import annotations

import sys
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


def test_routes_recall_reexports_extracted_model_classes():
    import recall_models
    import routes.recall as recall_route

    assert recall_route.RecallV2Response is recall_models.RecallV2Response
    assert recall_route.RecallResponse is recall_models.RecallResponse
    assert recall_route.RecallResult is recall_models.RecallResult
    assert recall_route.InjectionBlockModel is recall_models.InjectionBlockModel
    assert recall_route.CompoundRequest is recall_models.CompoundRequest


def test_routes_recall_reuses_extracted_response_cache_state():
    import recall_cache
    import routes.recall as recall_route

    recall_route._recall_cache.clear()
    response = recall_route.RecallV2Response(query="q", results=[], total_candidates=0)

    recall_route._recall_cache_put("cache-key", response)

    assert recall_route._recall_cache is recall_cache._recall_cache
    assert recall_cache._recall_cache_get("cache-key") is response
    assert recall_route._recall_cache_get("cache-key") is response

    recall_route._recall_cache.clear()


def test_routes_recall_embedding_cache_wrapper_uses_route_level_embedding_monkeypatch(monkeypatch):
    import routes.recall as recall_route

    calls: list[tuple[str, bool, str]] = []

    def fake_get_embedding(text: str, use_cache: bool, prefix: str) -> list[float]:
        calls.append((text, use_cache, prefix))
        return [1.0, 0.0, 0.0]

    monkeypatch.setattr(recall_route, "_get_embedding", fake_get_embedding)
    recall_route._recall_embedding_cache.clear()

    payload = {"ok": True}
    recall_route._recall_emb_cache_put("same query", payload)

    assert recall_route._recall_emb_cache_lookup("same query") is payload
    assert calls == [
        ("same query", True, "query"),
        ("same query", True, "query"),
    ]

    recall_route._recall_embedding_cache.clear()


def test_routes_recall_clear_caches_delegates_to_extracted_cache_state():
    import routes.recall as recall_route

    recall_route._recall_cache.clear()
    recall_route._recall_embedding_cache.clear()
    recall_route._recall_cache["k"] = (
        0.0,
        recall_route.RecallV2Response(query="q", results=[], total_candidates=0),
    )
    recall_route._recall_embedding_cache.append((0.0, [1.0], "q", {"ok": True}))

    assert recall_route.clear_caches() == {"recall_cache_cleared": 1, "embedding_cache_cleared": 1}
    assert recall_route._recall_cache == {}
    assert recall_route._recall_embedding_cache == []


def test_routes_recall_reexports_temporal_filter_helper():
    import recall_temporal
    import routes.recall as recall_route

    assert recall_route._apply_temporal_filter_inplace is recall_temporal._apply_temporal_filter_inplace
