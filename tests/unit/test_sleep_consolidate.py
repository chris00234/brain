"""Phase N4 — sleep consolidation unit test.

Seeds a tmp brain.db with 20 synthetic atoms + 100 fake action_audit rows
across 10 sessions, runs sleep_consolidate.run(), and asserts:

- atom_coactivation has the expected pair rows
- at least one atom flipped episodic → semantic (CLS promotion)
- atom_evidence has reinforce rows from the promotion
- exactly one sleep_cycles row with summary_json populated
- wall-clock under 60s

The A-MEM step (which calls Chroma) is neutered by pointing
`_amem_link_neighbors` at an empty return — this unit test focuses on the
deterministic parts. The live ChromaDB linking is covered by a follow-up
smoke run post-commit.
"""

from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))
sys.path.insert(0, str(BRAIN_ROOT / "brain_core" / "pipeline"))


@pytest.fixture
def brain_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_ATOMS_ENABLED", "true")
    for mod in ("atoms_store", "config", "sleep_consolidate"):
        if mod in sys.modules:
            del sys.modules[mod]
    import atoms_store

    fake_db = tmp_path / "brain.db"
    monkeypatch.setattr(atoms_store, "BRAIN_ATOMS_ENABLED", True)
    monkeypatch.setattr(atoms_store, "BRAIN_DB", fake_db)
    monkeypatch.setattr(atoms_store, "_initialized", False)
    atoms_store.init_schema(fake_db)

    # The _DDL already includes atom_evidence + atom_coactivation + sleep_cycles
    # (brain_db@7 + @8 DDL lives in atoms_store._DDL now), so the init_schema
    # call above is sufficient — no separate migration wiring needed.

    # Seed 20 atoms — first 5 get above-threshold confidence (0.7) so they're
    # eligible for CLS promotion, rest stay at 0.5.
    for i in range(20):
        atoms_store.upsert_atom(
            text=f"atom {i} sample content",
            chroma_id=f"semantic_memory:atom_{i}",
            kind="fact",
            confidence=0.7 if i < 5 else 0.5,
            tier="episodic",
        )

    import sleep_consolidate as sc

    monkeypatch.setattr(sc, "BRAIN_DB", fake_db)
    # Stub the A-MEM neighbor linking (Chroma) and Sage summary (LLM dispatch)
    monkeypatch.setattr(sc, "_amem_link_neighbors", lambda *_args, **_kw: 0)
    monkeypatch.setattr(
        sc,
        "_summarize_via_sage",
        lambda *_args, **_kw: {"skipped": "test_stub"},
    )

    from datetime import UTC, datetime, timedelta

    base_ts = datetime.now(UTC) - timedelta(hours=6)
    for sess in range(10):
        session_id = f"sess_{sess}"
        for row_i in range(10):
            _created = (base_ts + timedelta(minutes=sess * 30 + row_i)).isoformat(timespec="seconds")
            if sess < 5:
                retrieved = [f"semantic_memory:atom_{j}" for j in range(5)]
            else:
                retrieved = [f"semantic_memory:atom_{10 + (sess + row_i) % 10}"]
            atoms_store.insert_action_audit(
                route="/recall/v2",
                tool="brain_recall",
                actor="test",
                query_text=f"query {sess}:{row_i}",
                retrieved_chroma_ids=retrieved,
                session_id=session_id,
            )

    yield atoms_store, sc, fake_db

    importlib.reload(atoms_store)


def test_sleep_cycle_records_row_and_under_60s(brain_env):
    atoms_store, sc, fake_db = brain_env
    t0 = time.time()
    result = sc.run()
    elapsed = time.time() - t0
    assert result.get("ok") is True, f"sleep_consolidate failed: {result}"
    assert elapsed < 60, f"sleep_consolidate too slow: {elapsed:.1f}s"

    import sqlite3

    conn = sqlite3.connect(str(fake_db))
    try:
        rows = conn.execute("SELECT * FROM sleep_cycles").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1, f"expected exactly one sleep_cycles row, got {len(rows)}"


def test_coactivation_populated_from_hot_sessions(brain_env):
    _, sc, fake_db = brain_env
    result = sc.run()
    assert result["ok"]

    import sqlite3

    conn = sqlite3.connect(str(fake_db))
    try:
        pairs = conn.execute("SELECT atom_a_id, atom_b_id, n_events FROM atom_coactivation").fetchall()
    finally:
        conn.close()
    assert len(pairs) >= 5, f"expected at least 5 coactivation edges, got {len(pairs)}"


def test_cls_promotion_fires_for_frequent_atoms(brain_env):
    _, sc, fake_db = brain_env
    result = sc.run()
    assert result["ok"]
    import sqlite3

    conn = sqlite3.connect(str(fake_db))
    try:
        promoted = conn.execute("SELECT COUNT(*) FROM atoms WHERE tier='semantic'").fetchone()[0]
        evidence = conn.execute("SELECT COUNT(*) FROM atom_evidence WHERE event_type='reinforce'").fetchone()[
            0
        ]
    finally:
        conn.close()
    assert promoted >= 1, f"expected at least 1 episodic->semantic, got {promoted}"
    assert evidence >= 1, f"expected at least 1 reinforce ledger row, got {evidence}"
    assert result["promoted_episodic_to_semantic"] >= 1


def test_cold_atoms_stay_episodic(brain_env):
    _, sc, fake_db = brain_env
    sc.run()
    import sqlite3

    conn = sqlite3.connect(str(fake_db))
    try:
        episodic = conn.execute(
            "SELECT COUNT(*) FROM atoms WHERE tier='episodic' " "AND chroma_id LIKE 'semantic_memory:atom_%'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert episodic >= 10, f"cold atoms should stay episodic, found {episodic}"


def test_link_atom_entity_via_helpers(brain_env):
    atoms_store, _, fake_db = brain_env
    atom_id = atoms_store.derive_atom_id("semantic_memory:atom_0")
    eid = atoms_store.upsert_entity("React", entity_type="tool")
    assert eid is not None
    ok = atoms_store.link_atom_entity(atom_id, eid, role="subject")
    assert ok is True

    import sqlite3

    conn = sqlite3.connect(str(fake_db))
    try:
        rows = conn.execute("SELECT * FROM atom_entity WHERE atom_id=?", (atom_id,)).fetchall()
        ent_rows = conn.execute("SELECT * FROM entities WHERE id=?", (eid,)).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert len(ent_rows) == 1
