"""Quarantine path for query-keyed bridge atoms (Contract 8).

The cleanup CLI reuses the same governance classifier as the inventory, so
only classifier-positive bridge atoms can ever be mutated. It quarantines via
tier='obsolete' (the established auditable expiry tier) — never hard delete —
and every apply writes a full-row export that --revert can restore from.
"""

from __future__ import annotations

import json
import sqlite3

import atoms_store
from bridge_atom_cleanup import apply_cleanup, plan_cleanup, revert_cleanup

BRIDGE = 'Knowledge-gap bridge for query "memory dedup strategy": dedup uses simhash.'
EXACT = 'For the exact query "what is Chris email address": x@example.com.'
LEGIT = "Chris prefers light mode on all UIs and development tools."


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


def _rows(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM atoms")}
    conn.close()
    return rows


def test_plan_lists_only_classifier_positive_rows(tmp_path):
    db = tmp_path / "brain.db"
    _seed(db, [(BRIDGE, "episodic"), (LEGIT, "episodic"), (EXACT, "semantic")])
    plan = plan_cleanup(db)
    assert [r["id"] for r in plan] == ["atm_0", "atm_2"]
    before = _rows(db)
    assert before["atm_0"]["tier"] == "episodic"  # plan is read-only


def test_apply_quarantines_bridge_atoms_and_writes_export(tmp_path):
    db = tmp_path / "brain.db"
    export = tmp_path / "export.json"
    _seed(db, [(BRIDGE, "episodic"), (LEGIT, "episodic")])
    summary = apply_cleanup(db, export_path=export)
    assert summary["quarantined"] == ["atm_0"]
    rows = _rows(db)
    assert rows["atm_0"]["tier"] == "obsolete"
    assert rows["atm_1"]["tier"] == "episodic"  # legit row untouched
    prov = json.loads(rows["atm_0"]["provenance_json"])
    assert prov["bridge_cleanup"]["prior_tier"] == "episodic"
    assert prov["bridge_cleanup"]["reason"] == "query_keyed_bridge_quarantine"
    exported = json.loads(export.read_text())
    assert [r["id"] for r in exported["atoms"]] == ["atm_0"]
    assert exported["atoms"][0]["tier"] == "episodic"  # pre-mutation snapshot


def test_apply_skips_already_obsolete_rows(tmp_path):
    db = tmp_path / "brain.db"
    _seed(db, [(BRIDGE, "obsolete")])
    summary = apply_cleanup(db, export_path=tmp_path / "e.json")
    assert summary["quarantined"] == []


def test_apply_is_idempotent(tmp_path):
    db = tmp_path / "brain.db"
    _seed(db, [(BRIDGE, "episodic")])
    apply_cleanup(db, export_path=tmp_path / "e1.json")
    second = apply_cleanup(db, export_path=tmp_path / "e2.json")
    assert second["quarantined"] == []


def test_revert_restores_prior_tier_and_clears_marker(tmp_path):
    db = tmp_path / "brain.db"
    export = tmp_path / "export.json"
    _seed(db, [(BRIDGE, "episodic"), (EXACT, "semantic")])
    apply_cleanup(db, export_path=export)
    summary = revert_cleanup(db, export_path=export)
    assert sorted(summary["restored"]) == ["atm_0", "atm_1"]
    rows = _rows(db)
    assert rows["atm_0"]["tier"] == "episodic"
    assert rows["atm_1"]["tier"] == "semantic"
    assert "bridge_cleanup" not in json.loads(rows["atm_0"]["provenance_json"])


def test_revert_leaves_rows_obsoleted_by_other_paths(tmp_path):
    db = tmp_path / "brain.db"
    export = tmp_path / "export.json"
    _seed(db, [(BRIDGE, "episodic")])
    apply_cleanup(db, export_path=export)
    # Simulate an unrelated process re-obsoleting with its own provenance.
    conn = sqlite3.connect(str(db))
    with conn:
        conn.execute("UPDATE atoms SET provenance_json = '{}' WHERE id = 'atm_0'")
    conn.close()
    summary = revert_cleanup(db, export_path=export)
    assert summary["restored"] == []
    assert summary["skipped"] == [{"id": "atm_0", "reason": "no_cleanup_marker"}]
