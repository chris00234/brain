"""brain_core/brain_config_store.py — single source of truth for brain_config table.

The `brain_config` key/value table on autonomy.db is written by 5+ modules
(autonomy levels, autopilot state, quiet hours, denylist, SLO alert
suppression timestamps). Before this module each writer redeclared the
schema inline via `CREATE TABLE IF NOT EXISTS brain_config (...)` —
divergent column orders, missing PRAGMA WAL on some, retrofitted in-function
imports. This module collapses all of that to one helper.

Public API:
    ensure_schema()             — idempotent CREATE
    get(key) -> str | None      — single value lookup
    get_prefix(prefix) -> dict  — bulk fetch by key prefix
    set(key, value, updated_by) — INSERT … ON CONFLICT UPDATE
    delete(key) -> bool         — returns True if a row was removed

Errors are not swallowed — callers decide how to handle sqlite3.Error.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from config import AUTONOMY_DB
except ImportError:
    AUTONOMY_DB = Path("/Users/chrischo/server/brain/logs/autonomy.db")


_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS brain_config (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  updated_by TEXT DEFAULT 'system'
)
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    AUTONOMY_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(AUTONOMY_DB))
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure_schema() -> None:
    """Idempotent CREATE TABLE. Safe to call from any code path."""
    conn = _connect()
    try:
        conn.execute(_SCHEMA_DDL)
        conn.commit()
    finally:
        conn.close()


def get(key: str) -> str | None:
    """Return the value for one key, or None if absent."""
    ensure_schema()
    conn = _connect()
    try:
        row = conn.execute("SELECT value FROM brain_config WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def get_prefix(prefix: str) -> dict[str, str]:
    """Return {key: value} for all rows whose key starts with `prefix`.

    Equivalent to `WHERE key LIKE 'prefix%'`. Caller passes the literal
    prefix (no LIKE wildcards). Used by autonomy levels / denylist / SLO
    alert state, all of which encode multi-record state under a key prefix.
    """
    ensure_schema()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT key, value FROM brain_config WHERE key LIKE ?",
            (f"{prefix}%",),
        ).fetchall()
        return {r[0]: r[1] for r in rows}
    finally:
        conn.close()


def set(key: str, value: str, *, updated_by: str = "system") -> None:
    """Upsert a single key. `updated_at` is set automatically (UTC ISO8601)."""
    ensure_schema()
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO brain_config (key, value, updated_at, updated_by) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            " value=excluded.value, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (key, value, _now_iso(), updated_by),
        )
        conn.commit()
    finally:
        conn.close()


def delete(key: str) -> bool:
    """Delete one row. Returns True if a row was actually removed."""
    ensure_schema()
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM brain_config WHERE key = ?", (key,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
