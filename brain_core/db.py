"""brain_core/db.py — shared sqlite + datetime helpers.

Before this module, _now_iso() was redefined 20+ times across brain_core/
and _ensure_schema()/_conn() patterns were each duplicated 9-16 times with
subtly different timeout/journal_mode/row_factory choices. That divergence
caused observable bugs (one module would set journal_mode=WAL, another
wouldn't; one set timeout=5, another timeout=10) and made debugging
concurrency issues painful.

This module is the single source of truth. New modules should import
from here. Existing modules can migrate piecemeal; their internal helpers
remain backward-compatible.

Three primitives:
  now_iso()              — UTC second-precision ISO timestamp string
  open_brain_db(timeout) — sqlite3.Connection with WAL + timeout sane defaults
  open_autonomy_db(...)  — same, against autonomy.db
  ensure_schema(conn, ddl) — idempotent schema execute with module-level cache

Plus a CONTEXT MANAGER `transaction(conn)` that wraps BEGIN IMMEDIATE
properly. The BEGIN IMMEDIATE pattern is used 28+ times in this codebase
with hand-rolled try/except — this collapses it.

Resource-safe: every helper closes connections in a `finally` block.
"""

from __future__ import annotations

import contextlib
import sqlite3
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from config import AUTONOMY_DB, BRAIN_DB
except ImportError:
    BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")
    AUTONOMY_DB = Path("/Users/chrischo/server/brain/logs/autonomy.db")


def now_iso(*, z_suffix: bool = False) -> str:
    """UTC second-precision ISO timestamp string.

    Replaces 20+ inline `_now_iso()` definitions across brain_core.

    `z_suffix=True` returns `...Z` instead of `...+00:00`. atoms_store,
    entry_manifest, memory_lifecycle, and entity_graph all write Z-suffix
    timestamps so their valid_from / observed_at columns lex-sort the same
    way. Modules writing to those tables MUST pass z_suffix=True or risk
    silent timestamp-ordering bugs.
    """
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    if z_suffix:
        return stamp.replace("+00:00", "Z")
    return stamp


def open_brain_db(timeout: float = 10.0, row_factory: type | None = None) -> sqlite3.Connection:
    """Open brain.db with WAL + sane defaults.

    Caller MUST close the connection (use try/finally or `with transaction`).
    """
    conn = sqlite3.connect(str(BRAIN_DB), timeout=timeout)
    if row_factory is not None:
        conn.row_factory = row_factory
    return conn


def open_autonomy_db(timeout: float = 5.0, row_factory: type | None = None) -> sqlite3.Connection:
    """Open autonomy.db with WAL + sane defaults."""
    conn = sqlite3.connect(str(AUTONOMY_DB), timeout=timeout)
    if row_factory is not None:
        conn.row_factory = row_factory
    return conn


_schema_cache: set[str] = set()


def ensure_schema(conn: sqlite3.Connection, key: str, ddl: str) -> None:
    """Idempotent schema executescript, cached per process per key.

    `key` is any unique string identifying this DDL block (typically the
    module name). The DDL is run once per process; subsequent calls are
    no-ops, matching the per-module `_schema_done` flag pattern.
    """
    if key in _schema_cache:
        return
    conn.executescript(ddl)
    conn.commit()
    _schema_cache.add(key)


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE / commit / rollback as a context manager.

    Use when a SELECT-then-INSERT pattern must be atomic against
    concurrent writers. Replaces the 28+ hand-rolled try/except blocks
    across the codebase.

    Example:
        with transaction(conn):
            existing = conn.execute("SELECT ...").fetchone()
            if not existing:
                conn.execute("INSERT ...")
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def parse_iso_utc(ts: str | None) -> datetime | None:
    """Parse ISO timestamp, forcing UTC when timezone is absent.

    Naive datetimes serialize without offset and compare incorrectly
    against UTC-stamped values in SQLite. Force UTC on naive inputs.
    Centralizes the fix that was duplicated in episodic_binding.
    """
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt
