"""Contract tests for generic recall governance query classifiers."""

from __future__ import annotations

import sys
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


def test_routes_recall_reexports_generic_query_classifier_seams():
    import routes.recall as recall_route
    from recall_governance import generic_queries

    assert recall_route._GENERIC_RECIPE_QUERY_TOKENS is generic_queries.GENERIC_RECIPE_QUERY_TOKENS
    assert recall_route._RECIPE_RESULT_TOKENS is generic_queries.RECIPE_RESULT_TOKENS
    assert recall_route._is_generic_recipe_query is generic_queries.is_generic_recipe_query
    assert recall_route._is_recipe_result is generic_queries.is_recipe_result


def test_generic_recipe_query_detects_world_knowledge_recipe_prompts():
    from routes.recall import _is_generic_recipe_query

    assert _is_generic_recipe_query("give me a tomato pasta sauce recipe")
    assert _is_generic_recipe_query("tomato pasta sauce")
    assert _is_generic_recipe_query("recipe")
    assert not _is_generic_recipe_query("what recipe workflow did Chris prefer for Brain evals?")
    assert not _is_generic_recipe_query("")


def test_recipe_result_detects_recipe_content_tokens():
    from routes.recall import _is_recipe_result

    assert _is_recipe_result({"content": "Ingredients: tomato, garlic, basil, olive oil."})
    assert _is_recipe_result({"title": "Pasta sauce notes"})
    assert not _is_recipe_result({"content": "Chris prefers concise architecture reviews."})
