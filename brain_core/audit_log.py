"""Unified audit log for merge, conflict, dedup, and resolution decisions.

Stored in SQLite for structured queries and UI rendering.
Every dedup/merge/conflict decision in the Brain system should call log_event().

Test-isolation fix (2026-04-14): `BRAIN_AUDIT_DB` env var overrides the
default path so pytest fixtures can redirect logging to tmp_path. Without
this, test runs that triggered upsert_atom failures with mock DBs were
polluting the production `logs/audit.db` and inflating the
`atoms_write_fail_rate_1h` SLO (540 bogus failures in one hour from my
own CR5/CR6 dev scripts).
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    from config import BRAIN_LOGS_DIR
except ImportError:
    BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")


def _resolve_db_path() -> Path:
    """Resolve the audit DB path, honoring BRAIN_AUDIT_DB env override."""
    override = os.environ.get("BRAIN_AUDIT_DB")
    if override:
        return Path(override)
    return BRAIN_LOGS_DIR / "audit.db"


DB_PATH = _resolve_db_path()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    entity_a TEXT,
    entity_b TEXT,
    match_score REAL,
    conflict_type TEXT,
    resolution TEXT,
    reason TEXT,
    source_evidence TEXT,
    review_required INTEGER DEFAULT 0,
    reviewed_at TEXT,
    reviewed_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_type ON audit_events(event_type);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_review ON audit_events(review_required, reviewed_at);
"""


_schema_initialized = False
_schema_lock = threading.Lock()


def _active_db_path() -> Path:
    """Re-resolve DB path on each call so BRAIN_AUDIT_DB env overrides
    set after module import (e.g. by pytest fixtures) take effect."""
    override = os.environ.get("BRAIN_AUDIT_DB")
    if override:
        return Path(override)
    return DB_PATH


def _ensure_schema():
    global _schema_initialized
    path = _active_db_path()
    if _schema_initialized and path == DB_PATH:
        return
    with _schema_lock:
        if _schema_initialized and path == DB_PATH:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        conn.close()
        if path == DB_PATH:
            _schema_initialized = True


@contextlib.contextmanager
def _conn_ctx():
    """Short-lived connection — opens, yields, closes. Avoids thread-local
    leaks in long-running worker thread pools where threads come and go."""
    _ensure_schema()
    conn = sqlite3.connect(str(_active_db_path()))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_event(
    event_type: str,
    entity_a: str = "",
    entity_b: str = "",
    match_score: float = 0.0,
    conflict_type: str = "",
    resolution: str = "",
    reason: str = "",
    source_evidence: dict | None = None,
    review_required: bool = False,
    reviewed_by: str = "auto",
) -> str:
    """Log a dedup/merge/conflict/resolution event. Returns the event ID."""
    event_id = f"audit_{uuid.uuid4().hex[:12]}"
    with _conn_ctx() as conn:
        conn.execute(
            "INSERT INTO audit_events "
            "(id, timestamp, event_type, entity_a, entity_b, match_score, "
            " conflict_type, resolution, reason, source_evidence, "
            " review_required, reviewed_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id, _now(), event_type, entity_a, entity_b,
                match_score, conflict_type, resolution, reason,
                json.dumps(source_evidence or {}),
                1 if review_required else 0,
                reviewed_by if not review_required else "",
            ),
        )
        conn.commit()
    return event_id


def list_events(
    event_type: str | None = None,
    since: str | None = None,
    pending_only: bool = False,
    limit: int = 50,
) -> list[dict]:
    """Query audit events with optional filters."""
    clauses = []
    params: list = []
    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    if since:
        clauses.append("timestamp >= ?")
        params.append(since)
    if pending_only:
        clauses.append("review_required = 1 AND reviewed_at IS NULL")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _conn_ctx() as conn:
        rows = conn.execute(
            f"SELECT * FROM audit_events {where} ORDER BY timestamp DESC LIMIT ?",
            params + [limit],
        ).fetchall()
    return [dict(r) for r in rows]


def review_event(event_id: str, reviewed_by: str = "chris") -> bool:
    """Mark an audit event as reviewed."""
    with _conn_ctx() as conn:
        conn.execute(
            "UPDATE audit_events SET reviewed_at = ?, reviewed_by = ? WHERE id = ?",
            (_now(), reviewed_by, event_id),
        )
        conn.commit()
    return True


def stats() -> dict:
    """Return audit event counts by type."""
    with _conn_ctx() as conn:
        rows = conn.execute(
            "SELECT event_type, COUNT(*) as count FROM audit_events GROUP BY event_type"
        ).fetchall()
        pending = conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE review_required = 1 AND reviewed_at IS NULL"
        ).fetchone()[0]
    return {
        "by_type": {r["event_type"]: r["count"] for r in rows},
        "total": sum(r["count"] for r in rows),
        "pending_review": pending,
    }
