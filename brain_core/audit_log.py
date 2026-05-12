"""Unified audit log for merge, conflict, dedup, and resolution decisions.

Stored in SQLite for structured queries and UI rendering.
Every dedup/merge/conflict decision in the Brain system should call log_event().

Test-isolation fix (2026-04-14): `BRAIN_AUDIT_DB` env var overrides the
default path so pytest fixtures can redirect logging to tmp_path. Without
this, test runs that triggered upsert_atom failures with mock DBs were
polluting the production `logs/audit.db` and inflating the
`atoms_write_fail_rate_1h` SLO (540 bogus failures in one hour from my
own CR5/CR6 dev scripts).

2026-05-12: connection management consolidated through `db.open_audit_db`
+ `db.ensure_schema`. The BRAIN_AUDIT_DB env override is now handled in
db.py itself; tests should monkeypatch `db.AUDIT_DB` (clears
`db._schema_cache`) or set the env var.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db import ensure_schema as _ensure_schema_cached
from db import now_iso as _now
from db import open_audit_db

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


@contextlib.contextmanager
def _conn_ctx():
    """Short-lived connection — opens, yields, closes. Avoids thread-local
    leaks in long-running worker thread pools where threads come and go."""
    conn = open_audit_db(row_factory=sqlite3.Row)
    _ensure_schema_cached(conn, "audit_log", _SCHEMA)
    try:
        yield conn
    finally:
        conn.close()


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
                event_id,
                _now(),
                event_type,
                entity_a,
                entity_b,
                match_score,
                conflict_type,
                resolution,
                reason,
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
