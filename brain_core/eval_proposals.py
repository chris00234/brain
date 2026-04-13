"""brain_core/eval_proposals.py - candidate eval queries from prod feedback.

Phase 7: when /recall/feedback receives wrong_answer=true with an `expected`
field, this module records the query as a candidate for the eval suite.

Schema lives in autonomy.db (so we don't introduce yet another DB file).
A weekly job (eval_holdout_promote) scores candidates and surfaces the top-N
to Jenna for Telegram review; approved items get appended to eval_holdout.json.

Manual gate preserved — no unsupervised ground truth growth.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from config import AUTONOMY_DB
except ImportError:
    AUTONOMY_DB = Path("/Users/chrischo/server/brain/logs/autonomy.db")


_init_lock = threading.Lock()
_initialized = False


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _ensure_schema() -> None:
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        AUTONOMY_DB.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(AUTONOMY_DB))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS eval_proposals (
                  id TEXT PRIMARY KEY,
                  query TEXT NOT NULL,
                  expected TEXT NOT NULL,
                  expected_sources TEXT NOT NULL DEFAULT '[]',
                  source_event TEXT NOT NULL DEFAULT 'manual',
                  status TEXT NOT NULL DEFAULT 'candidate',
                  confidence REAL NOT NULL DEFAULT 0.5,
                  novelty_score REAL,
                  promoted_at TEXT,
                  reviewed_at TEXT,
                  created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_eval_proposals_status ON eval_proposals(status)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_eval_proposals_created ON eval_proposals(created_at)"
            )
            conn.commit()
        finally:
            conn.close()
        _initialized = True


def _conn() -> sqlite3.Connection:
    _ensure_schema()
    conn = sqlite3.connect(str(AUTONOMY_DB))
    conn.row_factory = sqlite3.Row
    return conn


def insert_proposal(
    *,
    query: str,
    expected: str,
    source_event: str = "manual",
    expected_sources: list[str] | None = None,
    confidence: float = 0.5,
) -> str | None:
    """Append a new eval candidate. Returns the proposal id."""
    if not query or not expected:
        return None
    pid = f"prop_{uuid.uuid4().hex[:12]}"
    import json as _json

    try:
        conn = _conn()
        try:
            conn.execute(
                "INSERT INTO eval_proposals (id, query, expected, expected_sources, "
                " source_event, status, confidence, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'candidate', ?, ?)",
                (
                    pid,
                    query[:500],
                    expected[:2000],
                    _json.dumps(expected_sources or []),
                    source_event,
                    confidence,
                    _now(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return pid
    except sqlite3.Error:
        return None


def list_candidates(*, status: str = "candidate", limit: int = 50) -> list[dict]:
    try:
        conn = _conn()
        try:
            rows = conn.execute(
                "SELECT * FROM eval_proposals WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def mark_status(proposal_id: str, status: str, *, novelty_score: float | None = None) -> bool:
    """Update a proposal's status (candidate → pending → promoted | rejected)."""
    if status not in ("candidate", "pending", "promoted", "rejected"):
        raise ValueError(f"invalid status: {status}")
    try:
        conn = _conn()
        try:
            if status == "promoted":
                conn.execute(
                    "UPDATE eval_proposals SET status = ?, promoted_at = ?, novelty_score = ? "
                    "WHERE id = ?",
                    (status, _now(), novelty_score, proposal_id),
                )
            elif status == "rejected":
                conn.execute(
                    "UPDATE eval_proposals SET status = ?, reviewed_at = ?, novelty_score = ? "
                    "WHERE id = ?",
                    (status, _now(), novelty_score, proposal_id),
                )
            else:
                conn.execute(
                    "UPDATE eval_proposals SET status = ?, novelty_score = ? WHERE id = ?",
                    (status, novelty_score, proposal_id),
                )
            conn.commit()
            return True
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def stats() -> dict:
    """Aggregate counts by status — for /brain/eval-proposals dashboards."""
    try:
        conn = _conn()
        try:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM eval_proposals GROUP BY status").fetchall()
            return {r["status"]: r["n"] for r in rows}
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
