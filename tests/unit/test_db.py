from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


def _reload(monkeypatch, tmp_path):
    if "db" in sys.modules:
        del sys.modules["db"]
    import db

    monkeypatch.setattr(db, "BRAIN_DB", tmp_path / "brain.db")
    monkeypatch.setattr(db, "AUTONOMY_DB", tmp_path / "autonomy.db")
    db._schema_cache.clear()
    return db


def test_now_iso_returns_utc_iso(tmp_path, monkeypatch):
    db = _reload(monkeypatch, tmp_path)
    ts = db.now_iso()
    parsed = datetime.fromisoformat(ts)
    assert parsed.tzinfo is not None
    assert parsed.tzinfo.utcoffset(parsed) == datetime.now(UTC).tzinfo.utcoffset(datetime.now(UTC))


def test_now_iso_default_is_plus_zero_zero(tmp_path, monkeypatch):
    """Default form (no z_suffix) must end with the explicit +00:00 offset."""
    db = _reload(monkeypatch, tmp_path)
    ts = db.now_iso()
    assert ts.endswith("+00:00"), f"expected +00:00 suffix, got {ts!r}"
    assert "Z" not in ts


def test_now_iso_z_suffix_returns_z_form(tmp_path, monkeypatch):
    """z_suffix=True must emit ...Z so it lex-compares with atoms_store /
    entry_manifest / entity_graph timestamps."""
    db = _reload(monkeypatch, tmp_path)
    ts = db.now_iso(z_suffix=True)
    assert ts.endswith("Z"), f"expected Z suffix, got {ts!r}"
    assert "+00:00" not in ts
    # Still parses as UTC
    parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None


def test_ensure_schema_is_idempotent_per_key(tmp_path, monkeypatch):
    db = _reload(monkeypatch, tmp_path)
    conn = sqlite3.connect(str(tmp_path / "brain.db"))
    try:
        db.ensure_schema(conn, "test", "CREATE TABLE IF NOT EXISTS t(id INT)")
        db.ensure_schema(conn, "test", "CREATE TABLE IF NOT EXISTS u(id INT)")  # second key
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        # First call ran; second was cached because key='test' was already registered
        assert "t" in names
        # u table should NOT exist because the cache short-circuited on the same key
        assert "u" not in names
    finally:
        conn.close()


def test_ensure_schema_separate_keys_both_run(tmp_path, monkeypatch):
    db = _reload(monkeypatch, tmp_path)
    conn = sqlite3.connect(str(tmp_path / "brain.db"))
    try:
        db.ensure_schema(conn, "key_a", "CREATE TABLE IF NOT EXISTS a(id INT)")
        db.ensure_schema(conn, "key_b", "CREATE TABLE IF NOT EXISTS b(id INT)")
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "a" in names
        assert "b" in names
    finally:
        conn.close()


def test_transaction_commits_on_success(tmp_path, monkeypatch):
    db = _reload(monkeypatch, tmp_path)
    conn = sqlite3.connect(str(tmp_path / "brain.db"))
    try:
        conn.execute("CREATE TABLE t(id INT)")
        conn.commit()
        with db.transaction(conn):
            conn.execute("INSERT INTO t(id) VALUES (1)")
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    finally:
        conn.close()


def test_transaction_rolls_back_on_exception(tmp_path, monkeypatch):
    db = _reload(monkeypatch, tmp_path)
    conn = sqlite3.connect(str(tmp_path / "brain.db"))
    try:
        conn.execute("CREATE TABLE t(id INT)")
        conn.commit()
        try:
            with db.transaction(conn):
                conn.execute("INSERT INTO t(id) VALUES (1)")
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    finally:
        conn.close()


def test_parse_iso_utc_forces_tz_on_naive(tmp_path, monkeypatch):
    db = _reload(monkeypatch, tmp_path)
    naive = db.parse_iso_utc("2026-05-12T02:00:00")
    assert naive is not None
    assert naive.tzinfo is UTC


def test_parse_iso_utc_preserves_offset(tmp_path, monkeypatch):
    db = _reload(monkeypatch, tmp_path)
    aware = db.parse_iso_utc("2026-05-12T02:00:00+00:00")
    assert aware is not None
    assert aware.tzinfo is not None


def test_parse_iso_utc_returns_none_on_invalid(tmp_path, monkeypatch):
    db = _reload(monkeypatch, tmp_path)
    assert db.parse_iso_utc(None) is None
    assert db.parse_iso_utc("") is None
    assert db.parse_iso_utc("not a date") is None
