"""Generic out-of-domain query classifiers for recall governance.

These helpers identify recipe-style world-knowledge prompts and recipe results.
They stay as pure functions rather than classes because they carry no mutable
state and are used as compatibility seams by ``routes.recall``.
"""

from __future__ import annotations

from .normalization import tokenize
from .query_analyzer import _PERSONAL_MEMORY_TOKENS
from .source_authority import result_text

GENERIC_RECIPE_QUERY_TOKENS = {"recipe", "tomato", "pasta", "sauce", "cook", "cooking", "make", "steps"}
RECIPE_RESULT_TOKENS = {
    "recipe",
    "tomato",
    "tomatoes",
    "pasta",
    "sauce",
    "garlic",
    "basil",
    "olive",
    "ingredients",
}


def is_generic_recipe_query(q: str) -> bool:
    tokens = tokenize(q)
    if not tokens or tokens & _PERSONAL_MEMORY_TOKENS:
        return False
    return "recipe" in tokens or len(tokens & GENERIC_RECIPE_QUERY_TOKENS) >= 3


def is_recipe_result(result: dict) -> bool:
    return bool(tokenize(result_text(result)) & RECIPE_RESULT_TOKENS)
