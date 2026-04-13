"""Unit tests for brain_core.atoms_store — Phase 3 SQLite truth layer."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


@pytest.fixture
def enabled_atoms(tmp_path, monkeypatch):
    """Import atoms_store with feature flag ON and tmp_path DB."""
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


@pytest.fixture
def disabled_atoms(tmp_path, monkeypatch):
    """Import atoms_store with feature flag OFF — every call should no-op."""
    monkeypatch.setenv("BRAIN_ATOMS_ENABLED", "false")
    if "atoms_store" in sys.modules:
        del sys.modules["atoms_store"]
    if "config" in sys.modules:
        del sys.modules["config"]
    import atoms_store

    monkeypatch.setattr(atoms_store, "BRAIN_ATOMS_ENABLED", False)
    yield atoms_store
    importlib.reload(atoms_store)


def test_disabled_returns_none(disabled_atoms):
    assert (
        disabled_atoms.upsert_atom(text="hello", chroma_id="test:abc") is None
    ), "disabled flag must short-circuit upsert"
    assert (
        disabled_atoms.insert_raw_event(
            event_id="raw_1", timestamp="2026-04-13", source_type="test", content="hi"
        )
        is None
    )
    assert disabled_atoms.reinforce("test:abc") is None
    assert disabled_atoms.count_atoms() == {"enabled": 0}


def test_init_schema_creates_tables(enabled_atoms, tmp_path):
    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "brain.db"))
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    conn.close()
    names = {r[0] for r in rows}
    expected = {
        "raw_events",
        "atoms",
        "entities",
        "atom_entity",
        "provenance",
        "action_audit",
    }
    assert expected.issubset(names), f"missing tables: {expected - names}"


def test_derive_atom_id_deterministic(enabled_atoms):
    a = enabled_atoms.derive_atom_id("hello world")
    b = enabled_atoms.derive_atom_id("hello world")
    c = enabled_atoms.derive_atom_id("hello other")
    assert a == b
    assert a != c
    assert a.startswith("atm_")


def test_upsert_atom_roundtrip(enabled_atoms):
    atom_id = enabled_atoms.upsert_atom(
        text="Chris prefers FastAPI.",
        chroma_id="semantic_memory:abc123",
        kind="preference",
        confidence=0.9,
    )
    assert atom_id is not None
    fetched = enabled_atoms.get_atom_by_chroma_id("semantic_memory:abc123")
    assert fetched is not None
    assert fetched["text"] == "Chris prefers FastAPI."
    assert fetched["kind"] == "preference"
    assert fetched["confidence"] == 0.9
    assert fetched["tier"] == "episodic"


def test_upsert_idempotent(enabled_atoms):
    enabled_atoms.upsert_atom(text="v1", chroma_id="x:1", confidence=0.5)
    enabled_atoms.upsert_atom(text="v2", chroma_id="x:1", confidence=0.7)
    fetched = enabled_atoms.get_atom_by_chroma_id("x:1")
    assert fetched["text"] == "v2"
    assert fetched["confidence"] == 0.7
    counts = enabled_atoms.count_atoms()
    assert counts["atoms_total"] == 1


def test_insert_raw_event_dedupes_on_content_hash(enabled_atoms):
    eid1 = enabled_atoms.insert_raw_event(
        event_id="raw_1", timestamp="2026-04-13", source_type="test", content="same payload"
    )
    eid2 = enabled_atoms.insert_raw_event(
        event_id="raw_2", timestamp="2026-04-13", source_type="test", content="same payload"
    )
    assert eid1 == "raw_1"
    assert eid2 is None, "second insert with identical content should dedupe"


def test_mark_superseded_chains(enabled_atoms):
    enabled_atoms.upsert_atom(text="old fact", chroma_id="x:old")
    enabled_atoms.upsert_atom(text="new fact", chroma_id="x:new")
    ok = enabled_atoms.mark_superseded("x:old", "x:new")
    assert ok is True
    old = enabled_atoms.get_atom_by_chroma_id("x:old")
    new = enabled_atoms.get_atom_by_chroma_id("x:new")
    assert old["superseded_by"] == new["id"]
    assert new["supersedes"] == old["id"]


def test_reinforce_increments_counter(enabled_atoms):
    enabled_atoms.upsert_atom(text="hello", chroma_id="x:1")
    result = enabled_atoms.reinforce("x:1", success=True)
    assert result is not None
    assert result["reinforcement_count"] == 1
    enabled_atoms.reinforce("x:1", success=True)
    enabled_atoms.reinforce("x:1", success=True)
    fetched = enabled_atoms.get_atom_by_chroma_id("x:1")
    assert fetched["reinforcement_count"] == 3


def test_count_atoms_reports_tier_distribution(enabled_atoms):
    enabled_atoms.upsert_atom(text="a", chroma_id="x:a", tier="episodic")
    enabled_atoms.upsert_atom(text="b", chroma_id="x:b", tier="semantic")
    enabled_atoms.upsert_atom(text="c", chroma_id="x:c", tier="core", canonical=True)
    counts = enabled_atoms.count_atoms()
    assert counts["atoms_total"] == 3
    assert counts["episodic"] == 1
    assert counts["semantic"] == 1
    assert counts["core"] == 1
    assert counts["canonical"] == 1


def test_action_audit_insert(enabled_atoms):
    rid = enabled_atoms.insert_action_audit(
        route="/recall/v2",
        query_text="test query",
        retrieved_atom_ids=["atm_001", "atm_002"],
        session_id="sess-1",
    )
    assert rid is not None
    import sqlite3

    conn = sqlite3.connect(str(enabled_atoms.BRAIN_DB))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM action_audit WHERE id = ?", (rid,)).fetchone()
    conn.close()
    assert row["query_text"] == "test query"
    assert row["session_id"] == "sess-1"
