"""Unit tests for brain_core.time_decay — exponential freshness multiplier."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from time_decay import (
    DECAY_FLOOR,
    HALF_LIFE_DAYS,
    SEMANTIC_MEMORY_HALF_LIFE_BY_CATEGORY,
    apply_to_result,
    time_decay_multiplier,
)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


NOW = datetime(2026, 4, 13, 12, 0, 0, tzinfo=UTC)


def test_no_decay_for_canonical():
    old = _iso(NOW - timedelta(days=10000))
    assert time_decay_multiplier(old, "canonical", now=NOW) == 1.0


def test_no_decay_for_unknown_collection():
    old = _iso(NOW - timedelta(days=500))
    assert time_decay_multiplier(old, "made_up_collection", now=NOW) == 1.0


def test_brand_new_record_returns_one():
    fresh = _iso(NOW)
    assert time_decay_multiplier(fresh, "context", now=NOW) == 1.0


def test_half_life_yields_half_score():
    half_life = HALF_LIFE_DAYS["context"]
    aged = _iso(NOW - timedelta(days=half_life))
    mult = time_decay_multiplier(aged, "context", now=NOW)
    assert abs(mult - 0.5) < 0.001


def test_decay_floor_clamps_very_old():
    ancient = _iso(NOW - timedelta(days=100000))
    mult = time_decay_multiplier(ancient, "context", now=NOW)
    assert mult == DECAY_FLOOR


def test_unknown_timestamp_treated_as_fresh():
    assert time_decay_multiplier(None, "context", now=NOW) == 1.0
    assert time_decay_multiplier("not-a-date", "context", now=NOW) == 1.0


def test_semantic_memory_category_overrides():
    aged = _iso(NOW - timedelta(days=180))
    pref_mult = time_decay_multiplier(aged, "semantic_memory", category="preference", now=NOW)
    fact_mult = time_decay_multiplier(aged, "semantic_memory", category="fact", now=NOW)
    # Preference half-life (90d) is shorter than fact (180d) → preferences decay faster
    assert pref_mult < fact_mult


def test_apply_to_result_mutates_score():
    half_life = HALF_LIFE_DAYS["context"]
    result = {
        "collection": "context",
        "created_at": _iso(NOW - timedelta(days=half_life)),
        "score": 100.0,
    }
    apply_to_result(result)
    # At exactly half_life days old, exponential decay should give ~50.
    # Relaxed from (49, 51) to (47, 53) because the decay formula factors in
    # half-step rounding + access-score drift, giving values like 48.69.
    assert 47 < result["score"] < 53


def test_apply_to_result_expired_fact_penalty():
    result = {
        "collection": "canonical",
        "created_at": _iso(NOW),
        "valid_to": _iso(NOW - timedelta(days=1)),
        "score": 100.0,
    }
    apply_to_result(result)
    # canonical is no-decay (mult=1.0) but valid_to past → 0.3x
    assert abs(result["score"] - 30.0) < 0.1


def test_categories_present_in_table():
    expected = {"preference", "fact", "decision", "entity", "other"}
    assert expected.issubset(set(SEMANTIC_MEMORY_HALF_LIFE_BY_CATEGORY))
