"""Behavioral unit tests for atoms_store.

Beyond the smoke import test, exercises the core write/supersession paths:
  - derive_atom_id / derive_content_hash: deterministic + collision-resistant
  - upsert_atom: round-trip + count visibility
  - mark_superseded: parent/child supersession with provenance edge
  - apply_explicit_replaces: explicit AI update path (D5 + 2026-04-26 contract)

Each test uses tmp_path-isolated brain.db so the production database is
never touched.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


@pytest.fixture(autouse=True)
def _enable_atoms(monkeypatch):
    """atoms_store gates writes on BRAIN_ATOMS_ENABLED; flip on for tests."""
    import atoms_store

    monkeypatch.setattr(atoms_store, "BRAIN_ATOMS_ENABLED", True)


def _fresh_db(tmp_path: Path) -> Path:
    from atoms_store import init_schema

    db = tmp_path / "brain.db"
    init_schema(db)
    return db


def test_derive_atom_id_is_deterministic_and_prefixed():
    from atoms_store import derive_atom_id

    a = derive_atom_id("hello world")
    b = derive_atom_id("hello world")
    c = derive_atom_id("hello world!")
    assert a == b
    assert a != c
    assert a.startswith("atm_")
    assert len(a) == len("atm_") + 12


def test_derive_content_hash_full_sha256():
    from atoms_store import derive_content_hash

    h = derive_content_hash("anything")
    assert len(h) == 64
    assert h == derive_content_hash("anything")
    assert h != derive_content_hash("anything ")


def test_upsert_atom_round_trip(tmp_path):
    from atoms_store import count_atoms, get_atom_by_chroma_id, upsert_atom

    db = _fresh_db(tmp_path)
    chroma_id = "test_chroma_001"
    atom_id = upsert_atom(
        text="The capital of France is Paris.",
        chroma_id=chroma_id,
        kind="fact",
        confidence=0.9,
        tier="semantic",
        db_path=db,
    )
    assert atom_id is not None
    assert atom_id.startswith("atm_")

    row = get_atom_by_chroma_id(chroma_id, db_path=db)
    assert row is not None
    assert row["text"] == "The capital of France is Paris."
    assert row["kind"] == "fact"
    assert row["tier"] == "semantic"
    assert abs(row["confidence"] - 0.9) < 1e-6

    counts = count_atoms(db_path=db)
    assert counts.get("semantic", 0) == 1


def test_upsert_atom_same_content_is_idempotent(tmp_path):
    """Content-identical atoms must collide at id level and update in place."""
    from atoms_store import count_atoms, upsert_atom

    db = _fresh_db(tmp_path)
    id1 = upsert_atom(text="duplicate content", chroma_id="cid_1", db_path=db)
    id2 = upsert_atom(text="duplicate content", chroma_id="cid_1", db_path=db)
    assert id1 == id2

    counts = count_atoms(db_path=db)
    assert counts["atoms_total"] == 1


def test_mark_superseded_writes_parent_child_and_provenance(tmp_path):
    from atoms_store import mark_superseded, upsert_atom

    db = _fresh_db(tmp_path)
    upsert_atom(text="old fact about X", chroma_id="parent_cid", db_path=db)
    upsert_atom(text="new updated fact about X", chroma_id="child_cid", db_path=db)

    ok = mark_superseded("parent_cid", "child_cid", db_path=db)
    assert ok is True

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        parent = conn.execute(
            "SELECT id, superseded_by FROM atoms WHERE chroma_id = ?",
            ("parent_cid",),
        ).fetchone()
        child = conn.execute(
            "SELECT id, supersedes FROM atoms WHERE chroma_id = ?",
            ("child_cid",),
        ).fetchone()
        assert parent["superseded_by"] == child["id"]
        assert child["supersedes"] == parent["id"]

        prov = conn.execute(
            "SELECT relation FROM provenance WHERE parent_id = ? AND child_id = ?",
            (parent["id"], child["id"]),
        ).fetchone()
        assert prov is not None
        assert prov["relation"] == "supersedes"
    finally:
        conn.close()


def test_mark_superseded_missing_atom_returns_false(tmp_path):
    from atoms_store import mark_superseded, upsert_atom

    db = _fresh_db(tmp_path)
    upsert_atom(text="exists", chroma_id="real_cid", db_path=db)
    assert mark_superseded("real_cid", "ghost_cid", db_path=db) is False
    assert mark_superseded("ghost_cid", "real_cid", db_path=db) is False


def test_apply_explicit_replaces_supersedes_targets(tmp_path):
    """Explicit AI update — skips cosine gate, applies direct supersession."""
    from atoms_store import apply_explicit_replaces, upsert_atom

    db = _fresh_db(tmp_path)
    old_id = upsert_atom(text="Chris uses tab indentation", chroma_id="old_cid", db_path=db)
    new_id = upsert_atom(text="Chris uses 4-space indentation", chroma_id="new_cid", db_path=db)
    assert old_id and new_id

    result = apply_explicit_replaces(
        "new_cid",
        [old_id],
        reason="preference change",
        agent="test",
        db_path=db,
    )
    assert result["applied"] == [old_id]
    assert result["skipped"] == []
    assert result["error"] is None

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        old_row = conn.execute(
            "SELECT superseded_by, valid_until FROM atoms WHERE id = ?",
            (old_id,),
        ).fetchone()
        new_row = conn.execute(
            "SELECT supersedes FROM atoms WHERE id = ?",
            (new_id,),
        ).fetchone()
        assert old_row["superseded_by"] == new_id
        assert old_row["valid_until"] is not None
        assert new_row["supersedes"] == old_id
    finally:
        conn.close()


def test_apply_explicit_replaces_skips_obsolete_and_unknown(tmp_path):
    from atoms_store import apply_explicit_replaces, upsert_atom

    db = _fresh_db(tmp_path)
    old_id = upsert_atom(
        text="already gone",
        chroma_id="obsolete_cid",
        tier="obsolete",
        db_path=db,
    )
    new_id = upsert_atom(text="replacement", chroma_id="new_cid", db_path=db)
    assert old_id and new_id

    result = apply_explicit_replaces(
        "new_cid",
        [old_id, "ghost_id_does_not_exist"],
        db_path=db,
    )
    assert result["applied"] == []
    skipped_reasons = {s["reason"] for s in result["skipped"]}
    assert "already_obsolete" in skipped_reasons
    assert "not_found" in skipped_reasons


def test_apply_explicit_replaces_unknown_new_atom_returns_error(tmp_path):
    from atoms_store import apply_explicit_replaces, upsert_atom

    db = _fresh_db(tmp_path)
    old_id = upsert_atom(text="existing", chroma_id="old_cid", db_path=db)
    assert old_id

    result = apply_explicit_replaces(
        "ghost_new_cid",
        [old_id],
        db_path=db,
    )
    assert result["applied"] == []
    assert result["error"] == "new_atom_not_found"


def test_apply_explicit_replaces_empty_target_list_is_noop(tmp_path):
    from atoms_store import apply_explicit_replaces

    db = _fresh_db(tmp_path)
    result = apply_explicit_replaces("any_cid", [], db_path=db)
    assert result == {"applied": [], "skipped": [], "error": None}
