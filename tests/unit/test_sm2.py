"""Unit tests for brain_core.sm2 — SuperMemo-2 spaced repetition."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


@pytest.fixture
def enabled_sm2(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_ATOMS_ENABLED", "true")
    for mod in ("atoms_store", "config", "sm2"):
        if mod in sys.modules:
            del sys.modules[mod]
    import atoms_store
    import sm2

    fake_db = tmp_path / "brain.db"
    monkeypatch.setattr(atoms_store, "BRAIN_ATOMS_ENABLED", True)
    monkeypatch.setattr(atoms_store, "BRAIN_DB", fake_db)
    monkeypatch.setattr(atoms_store, "_initialized", False)
    monkeypatch.setattr(sm2, "BRAIN_ATOMS_ENABLED", True)
    monkeypatch.setattr(sm2, "BRAIN_DB", fake_db)
    monkeypatch.setattr(sm2, "_conn", atoms_store._conn)
    monkeypatch.setattr(sm2, "get_atom_by_chroma_id", atoms_store.get_atom_by_chroma_id)
    atoms_store.init_schema(fake_db)
    yield sm2, atoms_store
    for mod in ("atoms_store", "config", "sm2"):
        if mod in sys.modules:
            del sys.modules[mod]


def test_schedule_first_correct_yields_one_day(enabled_sm2):
    sm2, _ = enabled_sm2
    state = sm2.schedule(easiness_factor=2.5, reinforcement_count=0, interval_days=0, quality=4)
    assert state["interval_days"] == 1.0
    assert state["reinforcement_count"] == 1


def test_schedule_second_correct_yields_six_days(enabled_sm2):
    sm2, _ = enabled_sm2
    state = sm2.schedule(easiness_factor=2.5, reinforcement_count=1, interval_days=1, quality=4)
    assert state["interval_days"] == 6.0
    assert state["reinforcement_count"] == 2


def test_schedule_third_uses_ef_multiplier(enabled_sm2):
    sm2, _ = enabled_sm2
    state = sm2.schedule(easiness_factor=2.5, reinforcement_count=2, interval_days=6, quality=4)
    assert state["interval_days"] == round(6 * state["easiness_factor"], 2)


def test_schedule_failure_resets(enabled_sm2):
    sm2, _ = enabled_sm2
    state = sm2.schedule(easiness_factor=2.5, reinforcement_count=10, interval_days=120, quality=1)
    assert state["interval_days"] == 1.0
    assert state["reinforcement_count"] == 0


def test_easiness_floor(enabled_sm2):
    sm2, _ = enabled_sm2
    # Five consecutive bad grades
    ef = 2.5
    for _ in range(20):
        state = sm2.schedule(easiness_factor=ef, reinforcement_count=0, interval_days=0, quality=0)
        ef = state["easiness_factor"]
    assert ef >= 1.3, "EF must never go below 1.3"


def test_apply_quality_persists(enabled_sm2):
    sm2, atoms_store = enabled_sm2
    atoms_store.upsert_atom(text="t", chroma_id="x:1")
    result = sm2.apply_quality("x:1", quality=4)
    assert result is not None
    assert result["reinforcement_count"] == 1
    fetched = atoms_store.get_atom_by_chroma_id("x:1")
    assert fetched["reinforcement_count"] == 1
    assert fetched["interval_days"] == 1.0


def test_promote_episodic_to_semantic(enabled_sm2):
    sm2, atoms_store = enabled_sm2
    atoms_store.upsert_atom(text="t", chroma_id="x:1", tier="episodic")
    # First two correct reviews: count=1 (interval 1), count=2 (interval 6) → eligible
    sm2.apply_quality("x:1", quality=4)
    result = sm2.apply_quality("x:1", quality=4)
    assert result["promoted"] is True
    assert result["tier"] == "semantic"


def test_promote_semantic_to_core_requires_canonical_zero(enabled_sm2):
    sm2, atoms_store = enabled_sm2
    atoms_store.upsert_atom(text="t", chroma_id="x:1", tier="semantic", canonical=False)
    # Bump up to count=5, interval >= 30
    for _ in range(6):
        sm2.apply_quality("x:1", quality=5)
    fetched = atoms_store.get_atom_by_chroma_id("x:1")
    assert fetched["tier"] == "core"


def test_review_due_returns_only_past_atoms(enabled_sm2):
    sm2, atoms_store = enabled_sm2
    atoms_store.upsert_atom(text="t", chroma_id="x:1")
    sm2.apply_quality("x:1", quality=4)
    # Newly-scheduled review is 1 day in the future; should NOT be due yet
    due = sm2.review_due()
    assert all(d["chroma_id"] != "x:1" for d in due), "fresh review should not be due"


def test_consolidate_obsolete_marks_stale(enabled_sm2, tmp_path, monkeypatch):
    sm2, atoms_store = enabled_sm2
    atoms_store.upsert_atom(text="stale", chroma_id="x:stale", tier="episodic")
    # Force an old next_review_at directly
    import sqlite3

    db_path = tmp_path / "brain.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE atoms SET next_review_at = ?, reinforcement_count = 0 WHERE chroma_id = 'x:stale'",
        ("2024-01-01T00:00:00+00:00",),
    )
    conn.commit()
    conn.close()
    result = sm2.consolidate_obsolete(days=30)
    assert result["obsoleted"] >= 1
    fetched = atoms_store.get_atom_by_chroma_id("x:stale")
    assert fetched["tier"] == "obsolete"


def test_nightly_pass_seeds_null_review(enabled_sm2):
    sm2, atoms_store = enabled_sm2
    atoms_store.upsert_atom(text="t", chroma_id="x:fresh")
    fetched = atoms_store.get_atom_by_chroma_id("x:fresh")
    assert fetched["next_review_at"] is None
    result = sm2.nightly_pass()
    assert result["seeded"] >= 1
    fetched = atoms_store.get_atom_by_chroma_id("x:fresh")
    assert fetched["next_review_at"] is not None


def test_disabled_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAIN_ATOMS_ENABLED", "false")
    for mod in ("atoms_store", "config", "sm2"):
        if mod in sys.modules:
            del sys.modules[mod]
    import sm2

    monkeypatch.setattr(sm2, "BRAIN_ATOMS_ENABLED", False)
    assert sm2.apply_quality("x:1", quality=4) is None
    assert sm2.review_due() == []
    assert sm2.consolidate_obsolete()["obsoleted"] == 0
