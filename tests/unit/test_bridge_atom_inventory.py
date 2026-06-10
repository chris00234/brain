"""Read-only inventory of query-keyed bridge atoms (Contract 7).

The inventory CLI reuses the recall-governance bridge classifier as its single
source of truth, so the report can never drift from what governance actually
penalizes. It opens the atoms DB read-only and NEVER mutates rows — cleanup
stays a separate, approval-gated step.
"""

from __future__ import annotations

import sqlite3

import atoms_store
from bridge_atom_inventory import inventory_bridge_atoms


def _seed(db_path, rows):
    atoms_store.init_schema(db_path)
    conn = sqlite3.connect(str(db_path))
    with conn:
        for i, (text, tier) in enumerate(rows):
            conn.execute(
                "INSERT INTO atoms (id, text, tier, chroma_id, valid_from, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, '2026-01-01', '2026-01-01', '2026-01-01')",
                (f"atm_{i}", text, tier, f"chr_{i}"),
            )
    conn.close()


def test_inventory_flags_only_bridge_atoms(tmp_path):
    db = tmp_path / "brain.db"
    _seed(
        db,
        [
            ('Knowledge-gap bridge for query "memory dedup strategy": dedup uses simhash.', "episodic"),
            ("Chris prefers light mode on all UIs and development tools.", "episodic"),
            ('For the exact query "what is Chris email address": x@example.com.', "episodic"),
        ],
    )
    flagged = inventory_bridge_atoms(db)
    assert [row["id"] for row in flagged] == ["atm_0", "atm_2"]
    assert all("text" in row and "tier" in row for row in flagged)


def test_inventory_skips_obsolete_tier(tmp_path):
    db = tmp_path / "brain.db"
    _seed(db, [('Knowledge-gap bridge for query "x": y.', "obsolete")])
    assert inventory_bridge_atoms(db) == []


def test_inventory_does_not_mutate_rows(tmp_path):
    db = tmp_path / "brain.db"
    _seed(db, [('For the query "a": b.', "episodic")])
    before = sqlite3.connect(str(db)).execute("SELECT id, text, tier FROM atoms").fetchall()
    inventory_bridge_atoms(db)
    after = sqlite3.connect(str(db)).execute("SELECT id, text, tier FROM atoms").fetchall()
    assert before == after
