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
    # Phase N2: upsert_atom freezes confidence on ON CONFLICT. Re-upserting
    # still updates text/kind/etc., but confidence is immutable from this
    # code path. update_atom_confidence is the ONLY mover — see
    # test_confidence_evidence.py::test_upsert_atom_freezes_confidence_on_conflict.
    enabled_atoms.upsert_atom(text="v1", chroma_id="x:1", confidence=0.5)
    enabled_atoms.upsert_atom(text="v2", chroma_id="x:1", confidence=0.7)
    fetched = enabled_atoms.get_atom_by_chroma_id("x:1")
    assert fetched["text"] == "v2"
    assert fetched["confidence"] == 0.5, "upsert must NOT overwrite confidence"
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


def test_concurrent_reinforce_no_lost_updates(enabled_atoms):
    """Regression: BEGIN IMMEDIATE on reinforce() must serialize concurrent writers.

    Without the explicit transaction, two threads reading reinforcement_count=N
    and both writing N+1 would lose one increment. With BEGIN IMMEDIATE the
    second writer blocks on the lock and re-reads the new value.
    """
    import sqlite3
    import threading

    enabled_atoms.upsert_atom(text="race target", chroma_id="race:1")

    n_threads = 10
    barrier = threading.Barrier(n_threads)
    errors: list[Exception] = []

    def worker():
        try:
            barrier.wait()
            enabled_atoms.reinforce("race:1", success=True)
        except Exception as exc:  # pragma: no cover — fail loud
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"reinforce raised: {errors}"
    conn = sqlite3.connect(str(enabled_atoms.BRAIN_DB))
    row = conn.execute("SELECT reinforcement_count FROM atoms WHERE chroma_id='race:1'").fetchone()
    conn.close()
    assert (
        row[0] == n_threads
    ), f"expected reinforcement_count={n_threads}, got {row[0]} (lost updates from race)"


def test_concurrent_mark_superseded_atomic(enabled_atoms):
    """Regression: BEGIN IMMEDIATE on mark_superseded must serialize the
    parent → child two-row flip. Without it, two writers racing on the same
    parent could leave the supersedes/superseded_by chain inconsistent.
    """
    import sqlite3
    import threading

    enabled_atoms.upsert_atom(text="parent atom", chroma_id="race:parent")
    # Create N distinct child atoms; threads each try to claim the parent.
    children = [f"race:child{i}" for i in range(8)]
    for c in children:
        enabled_atoms.upsert_atom(text=f"child {c}", chroma_id=c)

    barrier = threading.Barrier(len(children))
    errors: list[Exception] = []

    def worker(child_id: str):
        try:
            barrier.wait()
            enabled_atoms.mark_superseded("race:parent", child_id)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(c,)) for c in children]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"mark_superseded raised: {errors}"
    conn = sqlite3.connect(str(enabled_atoms.BRAIN_DB))
    conn.row_factory = sqlite3.Row
    parent = conn.execute("SELECT superseded_by FROM atoms WHERE chroma_id='race:parent'").fetchone()
    # The winning child's atom_id should equal the parent.superseded_by
    winning_child_id = parent["superseded_by"]
    assert winning_child_id is not None, "parent never got superseded_by set"
    # And THAT child's supersedes should point back at the parent
    parent_atom_id = conn.execute("SELECT id FROM atoms WHERE chroma_id='race:parent'").fetchone()[0]
    winner = conn.execute("SELECT supersedes FROM atoms WHERE id = ?", (winning_child_id,)).fetchone()
    conn.close()
    assert winner is not None and winner["supersedes"] == parent_atom_id, (
        "winning child's supersedes pointer doesn't match parent — race produced "
        "inconsistent two-row state"
    )


def test_update_provisional_flag_roundtrip(enabled_atoms):
    """Phase G2: write atom non-provisional, flip on, flip off, missing chroma_id."""
    import sqlite3

    enabled_atoms.upsert_atom(
        text="Beszel monitors hosts.",
        chroma_id="semantic_memory:prov1",
        kind="fact",
        confidence=0.7,
    )
    fetched = enabled_atoms.get_atom_by_chroma_id("semantic_memory:prov1")
    assert fetched is not None
    assert fetched["provisional"] == 0, "default upsert should land non-provisional"

    assert enabled_atoms.update_provisional_flag("semantic_memory:prov1", True) is True

    conn = sqlite3.connect(str(enabled_atoms.BRAIN_DB))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT provisional FROM atoms WHERE chroma_id = 'semantic_memory:prov1'").fetchone()
    conn.close()
    assert row["provisional"] == 1, "flag did not flip ON"

    assert enabled_atoms.update_provisional_flag("semantic_memory:prov1", False) is True

    conn = sqlite3.connect(str(enabled_atoms.BRAIN_DB))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT provisional FROM atoms WHERE chroma_id = 'semantic_memory:prov1'").fetchone()
    conn.close()
    assert row["provisional"] == 0, "flag did not flip OFF"

    # Unknown chroma_id → no row updated, returns False without raising.
    assert enabled_atoms.update_provisional_flag("semantic_memory:does_not_exist", True) is False
    # Empty chroma_id is a no-op, returns False.
    assert enabled_atoms.update_provisional_flag("", True) is False


def test_update_provisional_flag_disabled_returns_false(disabled_atoms):
    """Feature flag off → no DB I/O, returns False so callers can branch safely."""
    assert disabled_atoms.update_provisional_flag("semantic_memory:any", True) is False
