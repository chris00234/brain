"""answer_candidates — store high-value query answers for canonicalization.

Phase 3 of the llm-wiki adoption plan. When `/chris/think` produces a
first-person decision or any agent explicitly marks an answer as
load-bearing, we record it here. The nightly `answer_canonicalize` job
scores pending candidates and promotes the top N to the `raw/inbox/`
pipeline for eventual canonical promotion.

Schema (brain.db / answer_candidates):
    id, created_at, source_route, agent, query, answer, reason,
    score, status (pending|promoted|rejected|skipped), promoted_path,
    rejected_reason, processed_at
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

try:
    from config import BRAIN_DB
except ImportError:
    BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")

_DDL = """
CREATE TABLE IF NOT EXISTS answer_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    source_route TEXT NOT NULL,
    agent TEXT,
    query TEXT NOT NULL,
    answer TEXT NOT NULL,
    reason TEXT,
    score REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    promoted_path TEXT,
    rejected_reason TEXT,
    processed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_answer_candidates_status
    ON answer_candidates(status, created_at);
CREATE INDEX IF NOT EXISTS idx_answer_candidates_route
    ON answer_candidates(source_route, created_at);
"""


_schema_initialized = False
_schema_lock = __import__("threading").Lock()


def _conn() -> sqlite3.Connection:
    """Open a fresh connection. Callers MUST close it — prefer
    `with closing(_conn()) as conn:` to get deterministic cleanup since
    sqlite3's `with` block commits/rolls back but does NOT close.
    """
    global _schema_initialized
    BRAIN_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(BRAIN_DB), timeout=5)
    conn.row_factory = sqlite3.Row
    # 2026-04-18: previous version ran `executescript(_DDL)` on every _conn()
    # call (7 callers) — CREATE-TABLE-IF-NOT-EXISTS is idempotent but the
    # executescript implicitly commits, bumping DB write load for no reason.
    # Initialize schema once per process, protected by a lock to avoid the
    # read-modify-write race on the flag itself.
    if not _schema_initialized:
        with _schema_lock:
            if not _schema_initialized:
                conn.executescript(_DDL)
                _schema_initialized = True
    return conn


def record(
    source_route: str,
    query: str,
    answer: str,
    agent: str | None = None,
    reason: str | None = None,
) -> int:
    """Insert a new pending candidate. Returns row id.

    Caller is responsible for dedup — we don't check for near-duplicate
    queries here, the scorer handles that at promotion time.
    """
    if not query.strip() or not answer.strip():
        return 0
    if len(answer.strip()) < 80:
        return 0  # too short to be worth canonicalizing
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with closing(_conn()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO answer_candidates (created_at, source_route, agent, query, answer, reason) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (now, source_route, agent, query[:4000], answer[:16000], (reason or "")[:500]),
        )
        return cur.lastrowid or 0


def list_pending(limit: int = 50) -> list[dict]:
    with closing(_conn()) as conn, conn:
        rows = conn.execute(
            "SELECT * FROM answer_candidates WHERE status = 'pending' " "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get(candidate_id: int) -> dict | None:
    with closing(_conn()) as conn, conn:
        row = conn.execute("SELECT * FROM answer_candidates WHERE id = ?", (candidate_id,)).fetchone()
    return dict(row) if row else None


def mark_promoted(candidate_id: int, promoted_path: str, score: float) -> None:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with closing(_conn()) as conn, conn:
        conn.execute(
            "UPDATE answer_candidates SET status='promoted', promoted_path=?, "
            "score=?, processed_at=? WHERE id=?",
            (promoted_path, score, now, candidate_id),
        )


def mark_rejected(candidate_id: int, reason: str, score: float | None = None) -> None:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with closing(_conn()) as conn, conn:
        conn.execute(
            "UPDATE answer_candidates SET status='rejected', rejected_reason=?, "
            "score=?, processed_at=? WHERE id=?",
            (reason[:500], score, now, candidate_id),
        )


def mark_skipped(candidate_id: int, reason: str) -> None:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with closing(_conn()) as conn, conn:
        conn.execute(
            "UPDATE answer_candidates SET status='skipped', rejected_reason=?, " "processed_at=? WHERE id=?",
            (reason[:500], now, candidate_id),
        )


def stats() -> dict:
    with closing(_conn()) as conn, conn:
        rows = conn.execute("SELECT status, COUNT(*) as c FROM answer_candidates GROUP BY status").fetchall()
    return {r["status"]: r["c"] for r in rows}
