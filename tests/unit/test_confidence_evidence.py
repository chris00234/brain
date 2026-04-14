"""Phase N2 — mutable Bayesian confidence ledger.

Tests update_atom_confidence, get_confidence_history, rollback_confidence,
and the cluster_size_for LRU cache. Validates the ROME principle (localized +
reversible + verifiable) and Kuhn semantic-uncertainty normalization.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


@pytest.fixture
def atoms(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_ATOMS_ENABLED", "true")
    if "atoms_store" in sys.modules:
        del sys.modules["atoms_store"]
    if "config" in sys.modules:
        del sys.modules["config"]
    import atoms_store

    fake_db = tmp_path / "brain.db"
    monkeypatch.setattr(atoms_store, "BRAIN_ATOMS_ENABLED", True)
    monkeypatch.setattr(atoms_store, "BRAIN_DB", fake_db)
    monkeypatch.setattr(atoms_store, "_initialized", False)
    atoms_store.init_schema(fake_db)
    yield atoms_store
    importlib.reload(atoms_store)


def _seed_atom(atoms, confidence: float = 0.5) -> str:
    return atoms.upsert_atom(
        text="Chris prefers React for frontend",
        chroma_id="semantic_memory:test_seed",
        kind="preference",
        confidence=confidence,
    )


def test_update_moves_confidence_up_on_corroborate(atoms):
    atom_id = _seed_atom(atoms, confidence=0.5)
    result = atoms.update_atom_confidence(atom_id, "corroborate", 0.5)
    assert result is not None
    assert 0.55 < result["new_conf"] < 0.65
    assert result["old_conf"] == 0.5


def test_repeated_corroborate_is_monotone_and_clamped(atoms):
    atom_id = _seed_atom(atoms, confidence=0.5)
    prev = 0.5
    for _ in range(20):
        r = atoms.update_atom_confidence(atom_id, "corroborate", 0.5)
        assert r["new_conf"] >= prev - 1e-9
        assert r["new_conf"] <= 0.98
        prev = r["new_conf"]
    assert prev < 0.99


def test_contradict_drops_confidence(atoms):
    atom_id = _seed_atom(atoms, confidence=0.8)
    r = atoms.update_atom_confidence(atom_id, "contradict", -1.0)
    assert r is not None
    assert r["old_conf"] == pytest.approx(0.8, abs=1e-3)
    assert (r["old_conf"] - r["new_conf"]) >= 0.15


def test_history_returns_ledger_most_recent_first(atoms):
    atom_id = _seed_atom(atoms)
    atoms.update_atom_confidence(atom_id, "corroborate", 0.5)
    atoms.update_atom_confidence(atom_id, "contradict", -0.5)
    history = atoms.get_confidence_history(atom_id)
    assert len(history) == 2
    assert history[0]["event_type"] == "contradict"
    assert history[1]["event_type"] == "corroborate"


def test_rollback_replays_to_initial(atoms):
    atom_id = _seed_atom(atoms, confidence=0.5)
    atoms.update_atom_confidence(atom_id, "corroborate", 0.5)
    atoms.update_atom_confidence(atom_id, "corroborate", 0.5)
    before = atoms.get_atom_by_chroma_id("semantic_memory:test_seed")
    assert before["confidence"] > 0.6
    atoms.rollback_confidence(atom_id, back_to_event_id=0)
    after = atoms.get_atom_by_chroma_id("semantic_memory:test_seed")
    # Initial base is 0.5 → rollback_to=0 wipes every event, back to 0.5
    assert abs(after["confidence"] - 0.5) < 1e-6


def test_cluster_size_normalizes_weight(atoms):
    atom_id = _seed_atom(atoms, confidence=0.5)
    # Single cluster → full weight
    atoms.update_atom_confidence(atom_id, "corroborate", 0.5, cluster_size=1)
    one_round = atoms.get_atom_by_chroma_id("semantic_memory:test_seed")["confidence"]
    atoms.rollback_confidence(atom_id, back_to_event_id=0)
    # Cluster of 3 → weight divided by 3, confidence moves less
    atoms.update_atom_confidence(atom_id, "corroborate", 0.5, cluster_size=3)
    three_round = atoms.get_atom_by_chroma_id("semantic_memory:test_seed")["confidence"]
    assert abs(one_round - 0.5) > abs(three_round - 0.5) + 0.01


def test_disabled_event_type_noops(atoms):
    atom_id = _seed_atom(atoms)
    result = atoms.update_atom_confidence(atom_id, "unknown_type", 0.5)
    assert result is None
    history = atoms.get_confidence_history(atom_id)
    assert history == []


def test_upsert_atom_freezes_confidence_on_conflict(atoms):
    # First write at 0.5
    atoms.upsert_atom(
        text="Chris prefers React",
        chroma_id="semantic_memory:frozen_test",
        kind="preference",
        confidence=0.5,
    )
    # Move to 0.75 via ledger
    atom_id = atoms.derive_atom_id("semantic_memory:frozen_test")
    atoms.update_atom_confidence(atom_id, "corroborate", 0.5)
    atoms.update_atom_confidence(atom_id, "corroborate", 0.5)
    mid = atoms.get_atom_by_chroma_id("semantic_memory:frozen_test")["confidence"]
    assert mid > 0.6
    # Re-upsert with a different confidence — ON CONFLICT path must NOT overwrite
    atoms.upsert_atom(
        text="Chris prefers React",
        chroma_id="semantic_memory:frozen_test",
        kind="preference",
        confidence=0.1,  # would normally reset to 0.1
    )
    after = atoms.get_atom_by_chroma_id("semantic_memory:frozen_test")["confidence"]
    assert abs(after - mid) < 1e-6, (
        "upsert_atom ON CONFLICT must leave confidence untouched — "
        "update_atom_confidence is the only mover"
    )
