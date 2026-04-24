from __future__ import annotations

import importlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core"))

conflict_resolver = importlib.import_module("conflict_resolver")


NOW = datetime(2026, 4, 24, tzinfo=UTC)


def test_recommends_dismiss_for_missing_side() -> None:
    rec = conflict_resolver.recommend_resolution(
        {"distance": 0.2, "token_overlap": 0.3},
        {},
        {"confidence": 0.8},
        old_exists=False,
        new_exists=True,
        now=NOW,
    )

    assert rec.action == "dismiss"
    assert rec.auto_apply is True
    assert rec.review_required is False


def test_recommends_keep_active_over_superseded() -> None:
    rec = conflict_resolver.recommend_resolution(
        {},
        {"confidence": 0.9, "tier": "semantic", "superseded_by": "new"},
        {"confidence": 0.6, "tier": "episodic"},
        now=NOW,
    )

    assert rec.action == "keep_new"
    assert rec.auto_apply is True


def test_recommends_canonical_even_when_newer_is_less_authoritative() -> None:
    rec = conflict_resolver.recommend_resolution(
        {},
        {
            "confidence": 0.82,
            "trust_score": 0.9,
            "tier": "core",
            "kind": "decision",
            "canonical": True,
        },
        {"confidence": 0.75, "trust_score": 0.4, "tier": "episodic", "kind": "other"},
        now=NOW,
    )

    assert rec.action == "keep_old"
    assert rec.reason == "authority gap favors one side"
    assert rec.auto_apply is True


def test_recommends_needs_review_for_close_high_authority_conflict() -> None:
    old_meta = {"confidence": 0.82, "trust_score": 0.8, "tier": "semantic", "kind": "preference"}
    new_meta = {"confidence": 0.8, "trust_score": 0.82, "tier": "semantic", "kind": "preference"}

    rec = conflict_resolver.recommend_resolution(
        {"distance": 0.2, "token_overlap": 0.4, "created_at": NOW.isoformat()},
        old_meta,
        new_meta,
        now=NOW,
    )

    assert rec.action == "needs_review"
    assert rec.auto_apply is False
    assert rec.review_required is True


def test_recommends_keep_new_for_old_unreviewed_pending_conflict() -> None:
    rec = conflict_resolver.recommend_resolution(
        {"created_at": (NOW - timedelta(days=20)).isoformat()},
        {"confidence": 0.5, "tier": "episodic"},
        {"confidence": 0.5, "tier": "episodic"},
        now=NOW,
    )

    assert rec.action == "keep_new"
    assert rec.auto_apply is True
