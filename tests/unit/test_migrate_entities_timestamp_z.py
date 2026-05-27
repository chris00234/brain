"""tests/unit/test_migrate_entities_timestamp_z.py — P4-12 migration safety.

Verifies the brain_db v13→v14 migration that normalises `+00:00` →
`Z` timestamps on the entities table tolerates schema drift: a DB
initialised by `entity_graph`'s fallback DDL omits `neo4j_synced_at`,
and the migration must not crash with `no such column`.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


def _seed_narrow_entities(db_path: Path) -> None:
    """Recreate the entity_graph fallback schema (no neo4j_synced_at column)."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE entities (
                id              TEXT PRIMARY KEY,
                name            TEXT NOT NULL,
                entity_type     TEXT NOT NULL DEFAULT 'concept',
                first_seen_at   TEXT NOT NULL,
                last_seen_at    TEXT NOT NULL,
                mention_count   INTEGER NOT NULL DEFAULT 1
            );
            """
        )
        conn.executemany(
            "INSERT INTO entities (id, name, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?)",
            [
                ("e1", "foo", "2026-05-01T00:00:00+00:00", "2026-05-02T00:00:00+00:00"),
                ("e2", "bar", "2026-05-03T00:00:00Z", "2026-05-04T00:00:00Z"),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def test_v14_migration_tolerates_narrow_entities_schema(tmp_path, monkeypatch):
    db_path = tmp_path / "brain.db"
    _seed_narrow_entities(db_path)

    # Reload module under a patched BRAIN_DB so the migration helper
    # opens our temp DB, not the production one.
    import importlib

    for mod in ("migrations_brain_db", "atoms_store"):
        if mod in sys.modules:
            del sys.modules[mod]
    import migrations_brain_db as mb

    importlib.reload(mb)
    monkeypatch.setattr(mb, "_connect_brain_db", lambda: sqlite3.connect(str(db_path)))

    result = mb._normalize_entities_timestamps_to_z()
    brain_entities = result["brain_db.entities"]

    # neo4j_synced_at must not appear in the normalised set — column doesn't exist.
    assert "neo4j_synced_at" not in brain_entities["columns"]
    assert "first_seen_at" in brain_entities["columns"]
    assert "last_seen_at" in brain_entities["columns"]
    # Migration must have actually rewritten the +00:00 row.
    assert brain_entities["before_plus_offset"]["first_seen_at"] == 1
    assert brain_entities["after_plus_offset"]["first_seen_at"] == 0

    conn = sqlite3.connect(str(db_path))
    try:
        rows = list(conn.execute("SELECT id, first_seen_at, last_seen_at FROM entities ORDER BY id"))
    finally:
        conn.close()

    assert rows[0] == ("e1", "2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z")
    # e2 was already Z and stays as-is.
    assert rows[1] == ("e2", "2026-05-03T00:00:00Z", "2026-05-04T00:00:00Z")


def test_v14_migration_normalizes_neo4j_synced_at_when_present(tmp_path, monkeypatch):
    """When the column exists (atoms_store schema), it gets normalised too."""
    db_path = tmp_path / "brain.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE entities (
                id              TEXT PRIMARY KEY,
                name            TEXT NOT NULL,
                entity_type     TEXT NOT NULL DEFAULT 'concept',
                first_seen_at   TEXT NOT NULL,
                last_seen_at    TEXT NOT NULL,
                mention_count   INTEGER NOT NULL DEFAULT 1,
                neo4j_synced_at TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO entities (id, name, first_seen_at, last_seen_at, neo4j_synced_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "e1",
                "foo",
                "2026-05-01T00:00:00+00:00",
                "2026-05-02T00:00:00+00:00",
                "2026-05-03T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    import importlib

    for mod in ("migrations_brain_db",):
        if mod in sys.modules:
            del sys.modules[mod]
    import migrations_brain_db as mb

    importlib.reload(mb)
    monkeypatch.setattr(mb, "_connect_brain_db", lambda: sqlite3.connect(str(db_path)))

    result = mb._normalize_entities_timestamps_to_z()
    brain_entities = result["brain_db.entities"]
    assert set(brain_entities["columns"]) == {"first_seen_at", "last_seen_at", "neo4j_synced_at"}
    assert brain_entities["before_plus_offset"]["neo4j_synced_at"] == 1
    assert brain_entities["after_plus_offset"]["neo4j_synced_at"] == 0


def test_v15_migration_normalizes_autonomy_db_fallback(tmp_path, monkeypatch):
    """Codex review finding: v14 missed entity_graph's actual store
    (autonomy.db.entities + autonomy.db.memory_access). v15 backfills them.
    """
    brain_db = tmp_path / "brain.db"
    # Minimal brain.db for connect() to succeed.
    conn = sqlite3.connect(str(brain_db))
    conn.execute(
        "CREATE TABLE entities (id TEXT PRIMARY KEY, name TEXT, "
        "entity_type TEXT, first_seen_at TEXT, last_seen_at TEXT, "
        "mention_count INTEGER, neo4j_synced_at TEXT)"
    )
    conn.commit()
    conn.close()

    autonomy_db = tmp_path / "autonomy.db"
    conn = sqlite3.connect(str(autonomy_db))
    conn.executescript(
        """
        CREATE TABLE entities (
            id TEXT PRIMARY KEY, name TEXT, entity_type TEXT,
            first_seen_at TEXT, last_seen_at TEXT, mention_count INTEGER
        );
        CREATE TABLE memory_access (
            memory_id TEXT PRIMARY KEY, access_count INTEGER,
            last_accessed_at TEXT, first_accessed_at TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO entities (id, name, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?)",
        [("e_old", "x", "2026-05-01T00:00:00+00:00", "2026-05-02T00:00:00+00:00")],
    )
    conn.execute(
        "INSERT INTO memory_access (memory_id, access_count, last_accessed_at, first_accessed_at) "
        "VALUES (?, ?, ?, ?)",
        ("m1", 3, "2026-05-04T00:00:00+00:00", "2026-05-03T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    import importlib

    for mod in ("migrations_brain_db",):
        if mod in sys.modules:
            del sys.modules[mod]
    import migrations_brain_db as mb

    importlib.reload(mb)
    monkeypatch.setattr(mb, "BRAIN_DB", brain_db)

    result = mb._normalize_autonomy_db_timestamps_to_z()

    assert set(result["autonomy_db.entities"]["columns"]) == {"first_seen_at", "last_seen_at"}
    assert result["autonomy_db.entities"]["after_plus_offset"]["first_seen_at"] == 0
    assert result["autonomy_db.memory_access"]["after_plus_offset"]["first_accessed_at"] == 0

    conn = sqlite3.connect(str(autonomy_db))
    try:
        row = conn.execute("SELECT first_seen_at, last_seen_at FROM entities").fetchone()
        assert row == ("2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z")
        row = conn.execute("SELECT first_accessed_at, last_accessed_at FROM memory_access").fetchone()
        assert row == ("2026-05-03T00:00:00Z", "2026-05-04T00:00:00Z")
    finally:
        conn.close()


def test_v15_migration_noop_when_autonomy_db_missing(tmp_path, monkeypatch):
    brain_db = tmp_path / "brain.db"
    sqlite3.connect(str(brain_db)).close()

    import importlib

    for mod in ("migrations_brain_db",):
        if mod in sys.modules:
            del sys.modules[mod]
    import migrations_brain_db as mb

    importlib.reload(mb)
    monkeypatch.setattr(mb, "BRAIN_DB", brain_db)

    result = mb._normalize_autonomy_db_timestamps_to_z()
    assert result == {"autonomy_db_missing": True}
