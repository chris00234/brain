"""brain_core/claude_session.py — Claude Code session marker + queue scaffold.

Goal (2026-04-17 Chris directive): when Chris is in a Claude Code session,
route OpenAI-subscription LLM calls (distill, critique, gist, reflect) to
Claude directly instead of dispatching to Jenna. Scheduled/autonomous jobs
outside the session continue using Jenna as before. Preserves 100% coverage,
zero additional API cost during sessions, better quality (Claude is already
attending), and killed-job risk drops.

Architecture:
  1. SessionStart hook POSTs /brain/claude-session/start → stores timestamp
     in brain_config_store with a 10-minute TTL (extended by heartbeat).
  2. SessionEnd hook POSTs /brain/claude-session/end → clears the marker.
  3. `is_session_active()` reads the marker + TTL check.
  4. Sensitive call sites (self_rag.critique, hyde.expand_query, etc.) call
     `session_preferred_or(default_fn)` which short-circuits when session is
     active (returns a neutral result) instead of dispatching to Jenna.
  5. Future extension: `claude_llm_queue` SQLite table accepts queued requests
     that Claude drains on demand via /brain/claude-queue endpoints.
     Scaffold created but not yet wired to dispatch sites.

MVP (this module):
  - is_session_active(): bool
  - start_session(), extend_session(), end_session()
  - queue_for_claude(kind, prompt, payload) → queue_id
  - drain_pending(limit) → list of pending items
  - answer_item(queue_id, answer) → bool
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

log = logging.getLogger("brain.claude_session")

BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")
SESSION_TTL_SECONDS = 600  # 10 min — heartbeat extends

# Kinds that benefit from Claude-in-session handling.
# Kept conservative: these are live calls where a 2-5s Jenna dispatch is a
# visible latency AND the output is advisory (critique, expansion). Scheduled
# heavy jobs (distill on SessionEnd, memory_nudge Sunday) still route to
# Jenna — they run outside sessions anyway.
SESSION_PREFERRED_KINDS = frozenset(
    {
        "self_rag_critique",
        "hyde_expand",
        "canonical_merge_critic",
        "in_session_workflow_extract",
    }
)

# 2026-04-17 prod-review fix: one-time schema check instead of per-call
# open+CREATE+close. Module-level flag is safe — no threading concerns since
# the worst-case race is double-CREATE-IF-NOT-EXISTS which is idempotent.
_schema_done = False


def _ensure_schema() -> None:
    global _schema_done
    if _schema_done:
        return
    conn = sqlite3.connect(str(BRAIN_DB))
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS claude_llm_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                prompt TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',  -- pending | claimed | answered | expired
                created_at TEXT NOT NULL,
                claimed_at TEXT,
                answered_at TEXT,
                claimed_by TEXT,
                answer_text TEXT,
                answer_meta_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_claude_queue_status ON claude_llm_queue(status, created_at);
            CREATE INDEX IF NOT EXISTS idx_claude_queue_kind ON claude_llm_queue(kind, status);
            """
        )
        conn.commit()
        _schema_done = True
    finally:
        conn.close()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _now_ts() -> float:
    return time.time()


# ── Session marker (stored in brain_config_store for persistence across reload) ─

_SESSION_KEY = "claude_session.active"


def _store_get(key: str) -> str | None:
    try:
        import brain_config_store

        return brain_config_store.get(key)
    except Exception:
        return None


def _store_set(key: str, val: str) -> None:
    try:
        import brain_config_store

        brain_config_store.set(key, val, updated_by="claude_session")
    except Exception as _exc:
        log.debug("silenced exception in claude_session.py: %s", _exc)


def start_session(claude_session_id: str = "") -> dict:
    """SessionStart hook calls this. Sets the active marker with TTL.

    2026-04-17 prod-review: all timestamp fields are ISO strings for uniformity
    and observability. expires_at_ts retains epoch float for fast comparison
    in is_session_active (avoids datetime parsing on hot path).
    """
    _ensure_schema()
    now_ts = _now_ts()
    payload = {
        "started_at": _now_iso(),
        "expires_at": datetime.fromtimestamp(now_ts + SESSION_TTL_SECONDS, UTC).isoformat(timespec="seconds"),
        "expires_at_ts": now_ts + SESSION_TTL_SECONDS,
        "session_id": claude_session_id or "",
        "heartbeat_at": _now_iso(),
    }
    _store_set(_SESSION_KEY, json.dumps(payload))
    return {"ok": True, **payload}


def extend_session() -> dict:
    """Heartbeat — extends TTL. Called periodically by Claude Code during work."""
    raw = _store_get(_SESSION_KEY)
    if not raw:
        return start_session()
    try:
        payload = json.loads(raw)
    except Exception:
        return start_session()
    now_ts = _now_ts()
    payload["expires_at"] = datetime.fromtimestamp(now_ts + SESSION_TTL_SECONDS, UTC).isoformat(
        timespec="seconds"
    )
    payload["expires_at_ts"] = now_ts + SESSION_TTL_SECONDS
    payload["heartbeat_at"] = _now_iso()
    _store_set(_SESSION_KEY, json.dumps(payload))
    return {"ok": True, **payload}


def end_session() -> dict:
    """SessionEnd hook calls this. Clears the marker."""
    _store_set(_SESSION_KEY, "")
    return {"ok": True, "ended_at": _now_iso()}


def is_session_active() -> bool:
    """True when a non-expired session marker exists. Reads expires_at_ts
    (epoch float) for fast comparison on the hot path.
    """
    raw = _store_get(_SESSION_KEY)
    if not raw:
        return False
    try:
        payload = json.loads(raw)
        exp = float(payload.get("expires_at_ts") or 0)
        return _now_ts() < exp
    except Exception:
        return False


def session_info() -> dict:
    """Return the session payload (for observability)."""
    raw = _store_get(_SESSION_KEY)
    if not raw:
        return {"active": False}
    try:
        payload = json.loads(raw)
        exp = float(payload.get("expires_at_ts") or 0)
        return {
            "active": _now_ts() < exp,
            "expires_in_s": max(0, int(exp - _now_ts())),
            **payload,
        }
    except Exception:
        return {"active": False, "error": "malformed_payload"}


# ── Queue (scaffold for future extension) ────────────────────────────────────


def queue_for_claude(kind: str, prompt: str, payload: dict | None = None) -> int:
    """Enqueue an LLM request for Claude to handle.

    Returns the queue_id. Callers can poll for the answer via get_answer()
    or accept a fallback path.
    """
    _ensure_schema()
    conn = sqlite3.connect(str(BRAIN_DB))
    try:
        cur = conn.execute(
            """INSERT INTO claude_llm_queue (kind, prompt, payload_json, status, created_at)
               VALUES (?, ?, ?, 'pending', ?)""",
            (kind, prompt[:20000], json.dumps(payload or {})[:10000], _now_iso()),
        )
        queue_id = cur.lastrowid
        conn.commit()
        return queue_id
    finally:
        conn.close()


def drain_pending(limit: int = 10, kinds: list[str] | None = None) -> list[dict]:
    """Claude pulls pending items (atomically claimed) to work on.

    2026-04-17 prod-review fix: wraps SELECT + UPDATE in BEGIN IMMEDIATE so
    concurrent drain callers can't claim the same items. SQLite's default
    auto-commit mode doesn't hold a lock across separate statements; two
    processes calling drain_pending in parallel would each see the same
    pending rows before either committed. BEGIN IMMEDIATE takes a write
    lock at transaction start, serializing the drain.
    """
    _ensure_schema()
    conn = sqlite3.connect(str(BRAIN_DB))
    try:
        conn.execute("BEGIN IMMEDIATE")
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            rows = conn.execute(
                f"SELECT id, kind, prompt, payload_json, created_at FROM claude_llm_queue "
                f"WHERE status = 'pending' AND kind IN ({placeholders}) "
                f"ORDER BY created_at ASC LIMIT ?",
                (*kinds, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, kind, prompt, payload_json, created_at FROM claude_llm_queue "
                "WHERE status = 'pending' ORDER BY created_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
        ids = [r[0] for r in rows]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE claude_llm_queue SET status = 'claimed', claimed_at = ?, "
                f"claimed_by = 'claude' WHERE id IN ({placeholders})",
                (_now_iso(), *ids),
            )
        conn.commit()
        return [
            {
                "id": r[0],
                "kind": r[1],
                "prompt": r[2],
                "payload": json.loads(r[3] or "{}"),
                "created_at": r[4],
            }
            for r in rows
        ]
    finally:
        conn.close()


def answer_item(queue_id: int, answer_text: str, meta: dict | None = None) -> bool:
    _ensure_schema()
    conn = sqlite3.connect(str(BRAIN_DB))
    try:
        # 2026-04-17 prod-review fix: cur.rowcount is the rowcount of the last
        # statement. conn.total_changes is cumulative across the connection
        # lifetime and falsely reports success on brain.db with any prior writes.
        cur = conn.execute(
            """UPDATE claude_llm_queue
               SET status = 'answered', answered_at = ?, answer_text = ?, answer_meta_json = ?
               WHERE id = ? AND status IN ('claimed', 'pending')""",
            (_now_iso(), answer_text[:20000], json.dumps(meta or {})[:5000], queue_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_answer(queue_id: int) -> dict | None:
    _ensure_schema()
    conn = sqlite3.connect(str(BRAIN_DB))
    try:
        row = conn.execute(
            "SELECT id, kind, status, answer_text, answer_meta_json, answered_at "
            "FROM claude_llm_queue WHERE id = ?",
            (queue_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "kind": row[1],
            "status": row[2],
            "answer": row[3] or "",
            "meta": json.loads(row[4] or "{}"),
            "answered_at": row[5] or "",
        }
    finally:
        conn.close()


def queue_stats() -> dict:
    _ensure_schema()
    conn = sqlite3.connect(str(BRAIN_DB))
    try:
        rows = conn.execute("SELECT status, count(*) FROM claude_llm_queue GROUP BY status").fetchall()
        return {r[0]: r[1] for r in rows}
    finally:
        conn.close()


def expire_stale(max_age_seconds: int = 3600) -> int:
    """Mark claimed-but-unanswered items as expired after N seconds."""
    _ensure_schema()
    conn = sqlite3.connect(str(BRAIN_DB))
    try:
        cutoff_ts = _now_ts() - max_age_seconds
        # SQLite doesn't have native timestamp arithmetic in query-safe way,
        # use datetime() function against claimed_at string.
        cur = conn.execute(
            "UPDATE claude_llm_queue SET status = 'expired' "
            "WHERE status = 'claimed' "
            "AND strftime('%s', claimed_at) < ?",
            (str(int(cutoff_ts)),),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("start").add_argument("--id", default="")
    sub.add_parser("end")
    sub.add_parser("info")
    sub.add_parser("stats")
    args = p.parse_args()
    if args.cmd == "start":
        print(json.dumps(start_session(args.id), indent=2))
    elif args.cmd == "end":
        print(json.dumps(end_session(), indent=2))
    elif args.cmd == "info":
        print(json.dumps(session_info(), indent=2))
    elif args.cmd == "stats":
        print(json.dumps(queue_stats(), indent=2))
    else:
        p.print_help()
