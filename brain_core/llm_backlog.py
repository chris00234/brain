"""brain_core/llm_backlog.py — unified catch-up queue for LLM-dependent work.

When OpenAI + fallbacks are all exhausted (or the llm.dispatch circuit
breaker is open for any reason), every pipeline that needs LLM work must
degrade gracefully — but without a queue, the dropped work is gone forever.
This module is the queue.

Seven kinds of work are enqueued:

  classify   — ingest_classifier LLM path (topic/speaker/scope)
  entities   — entity_graph.extract_and_store_entities
  distill    — canonical_pipeline inbox → distilled
  synthesis  — daily/weekly/monthly narrative writers
  proactive  — proactive.get_current_insights sweep
  telegram   — Jenna Telegram alert (time-sensitive, has TTL)
  reflect    — brain_reflect contradiction detection

Design:

  1. Table lives in autonomy.db (the existing "brain state that survives
     server restarts" file). DDL is idempotent.
  2. Enqueue is cheap: (kind, content_hash) UNIQUE so same work isn't
     queued twice. Failed queue inserts are swallowed (best-effort).
  3. Drain runs every 30 min via scheduler cron AND on-demand when
     brain_loop detects the llm.dispatch breaker has transitioned
     open → closed (event-driven catch-up within 60 s of quota returning).
  4. Each kind has a handler function that re-runs the work. Handlers
     check the breaker first and abort the drain if LLM is still down.
  5. Telegram has a TTL (severity-dependent) so stale alerts don't spam
     Chris when brain catches up 12 hours later.
  6. SLO: `llm_backlog_pending > 100` warn, `oldest_pending_age > 24h` warn.

Public API:

  enqueue(kind, payload, content_hash=None) -> int | None
  drain(limit=50, abort_on_breaker=True) -> dict
  pending_count() -> int
  stats() -> dict
  register_handler(kind, fn) — for tests / dynamic wiring

Handlers are registered at import time by the modules that care. Defaults
are wired in wire_default_handlers() which this module calls on first
drain invocation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import sys
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

try:
    from config import AUTONOMY_DB
except ImportError:
    AUTONOMY_DB = Path("/Users/chrischo/server/brain/logs/autonomy.db")

log = logging.getLogger("brain.llm_backlog")

VALID_KINDS = {
    "classify",
    "entities",
    "distill",
    "synthesis",
    "proactive",
    "telegram",
    "reflect",
}

# Per-kind TTL in seconds — entries older than this are abandoned rather
# than retried. Telegram has the tightest TTL because a 12-hour-old alert
# is noise, not signal. Content work has long TTL because it's idempotent.
KIND_TTL_SECONDS = {
    "classify":  7 * 24 * 3600,   # a week
    "entities":  7 * 24 * 3600,
    "distill":   3 * 24 * 3600,
    "synthesis": 2 * 24 * 3600,
    "proactive": 12 * 3600,       # 12h
    "telegram":  6 * 3600,        # 6h — critical alerts
    "reflect":   3 * 24 * 3600,
}

# Max retries before abandoning. After N failed drains the entry is marked
# failed to prevent a pathological payload from blocking the queue forever.
MAX_RETRIES = 5

# Drain-time default cap so a burst drain doesn't stall for hours.
DEFAULT_DRAIN_LIMIT = 50

_handlers: dict[str, Callable[[dict], bool]] = {}
_handlers_wired = False


# ── Schema ────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS llm_backlog (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT    NOT NULL,
    payload_json   TEXT    NOT NULL,
    content_hash   TEXT    NOT NULL,
    created_at     TEXT    NOT NULL,
    last_attempt_at TEXT,
    retry_count    INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    status         TEXT    NOT NULL DEFAULT 'pending'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_llm_backlog_dedupe
    ON llm_backlog(kind, content_hash);
CREATE INDEX IF NOT EXISTS idx_llm_backlog_status_created
    ON llm_backlog(status, created_at);
"""

_schema_ready = False
_schema_lock = None


def _ensure_schema() -> None:
    global _schema_ready, _schema_lock
    if _schema_ready:
        return
    if _schema_lock is None:
        import threading
        _schema_lock = threading.Lock()
    with _schema_lock:
        if _schema_ready:
            return
        try:
            with sqlite3.connect(str(AUTONOMY_DB), timeout=5.0) as conn:
                conn.executescript(_DDL)
                conn.commit()
            _schema_ready = True
        except sqlite3.Error as e:
            log.warning("llm_backlog schema init failed: %s", e)


@contextmanager
def _connect():
    _ensure_schema()
    conn = sqlite3.connect(str(AUTONOMY_DB), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash_payload(kind: str, payload: dict) -> str:
    """Stable content hash for dedupe. Sorted-key JSON so equivalent payloads
    produce the same hash regardless of insert order."""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(f"{kind}:{canonical}".encode()).hexdigest()[:24]


# ── Public API: enqueue ───────────────────────────────────────────

def enqueue(kind: str, payload: dict, content_hash: str | None = None) -> int | None:
    """Add work to the backlog. Returns the row id, or None on failure
    (duplicate, DB error, invalid kind). Best-effort — never raises to
    the caller so failing to enqueue can't break the path that failed to
    reach LLM in the first place.
    """
    if kind not in VALID_KINDS:
        log.warning("llm_backlog.enqueue invalid kind=%s", kind)
        return None
    if content_hash is None:
        content_hash = _hash_payload(kind, payload)

    try:
        with _connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO llm_backlog "
                "(kind, payload_json, content_hash, created_at, status) "
                "VALUES (?, ?, ?, ?, 'pending')",
                (kind, json.dumps(payload, ensure_ascii=False), content_hash, _now_iso()),
            )
            if cursor.rowcount == 0:
                # Duplicate (already queued) — still counts as successful
                # because the work is tracked. Return existing id.
                row = conn.execute(
                    "SELECT id FROM llm_backlog WHERE kind=? AND content_hash=?",
                    (kind, content_hash),
                ).fetchone()
                return int(row["id"]) if row else None
            return int(cursor.lastrowid)
    except sqlite3.Error as e:
        log.warning("llm_backlog.enqueue failed: %s", e)
        return None


# ── Public API: handler registration ─────────────────────────────

def register_handler(kind: str, fn: Callable[[dict], bool]) -> None:
    """Register a handler for a backlog kind. fn(payload) → bool (True=done)."""
    if kind not in VALID_KINDS:
        raise ValueError(f"invalid backlog kind: {kind}")
    _handlers[kind] = fn


def _wire_default_handlers() -> None:
    """Lazy-wire the default handlers on first drain. Done lazily because
    these imports are heavy (pull in the full brain pipeline)."""
    global _handlers_wired
    if _handlers_wired:
        return
    _handlers_wired = True

    # ── classify ────────────────────────────────────────────
    def _handle_classify(payload: dict) -> bool:
        try:
            from ingest_classifier import classify
            # CR6 fix: force_llm=True bypasses the per-content cache so
            # a backlog retry actually hits the LLM. Without it, the
            # cached heuristic result is served and the handler can
            # never upgrade the classification.
            cls = classify(
                payload.get("content", ""),
                author_agent=payload.get("author_agent", "claude"),
                category=payload.get("category", "fact"),
                use_llm=True,
                force_llm=True,
            )
            if cls is None or cls.source != "llm":
                return False  # LLM still down
            # Update atom's hygiene fields
            atom_id = payload.get("atom_id")
            if not atom_id:
                return True  # no-op atom — treat as done
            # MR5 fix: use config.BRAIN_DB instead of hardcoded path
            try:
                from config import BRAIN_DB
            except ImportError:
                BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")
            with sqlite3.connect(str(BRAIN_DB), timeout=10.0) as conn:
                conn.execute(
                    "UPDATE atoms SET topic_key=?, speaker_entity=?, scope=?, "
                    "  provisional=?, trust_score=?, updated_at=? WHERE id=?",
                    (
                        cls.topic_key,
                        cls.speaker_entity,
                        cls.scope,
                        1 if cls.provisional else 0,
                        cls.confidence,
                        _now_iso(),
                        atom_id,
                    ),
                )
                conn.commit()
            return True
        except Exception as e:
            log.debug("handle_classify failed: %s", e)
            return False

    # ── entities ────────────────────────────────────────────
    def _handle_entities(payload: dict) -> bool:
        # CR8 fix (2026-04-14): extract_and_store_entities returns -1 on
        # LLM dispatch failure (rate-limited/breaker/timeout/parse error
        # on empty) and >=0 on success. Previously the handler ignored
        # the return and always returned True, marking LLM-failed entries
        # done without retry. Now we distinguish and let the drain loop
        # retry on -1.
        try:
            from entity_graph import extract_and_store_entities
            n = extract_and_store_entities(
                payload.get("text", "")[:1500],
                payload.get("chroma_id", ""),
            )
            if n < 0:
                return False  # LLM down, keep pending
            return True
        except Exception as e:
            log.debug("handle_entities failed: %s", e)
            return False

    # ── distill ─────────────────────────────────────────────
    def _handle_distill(payload: dict) -> bool:
        try:
            from openclaw_dispatch import dispatch
            result = dispatch(
                agent="jenna",
                message=payload.get("prompt", ""),
                thinking="low",
                timeout=payload.get("timeout", 90),
            )
            if not result.ok:
                return False
            # Write the distilled output to the inbox path if provided
            out_path = payload.get("out_path")
            if out_path:
                Path(out_path).write_text(result.text, encoding="utf-8")
            return True
        except Exception as e:
            log.debug("handle_distill failed: %s", e)
            return False

    # ── synthesis ───────────────────────────────────────────
    def _handle_synthesis(payload: dict) -> bool:
        """MR6 fix (2026-04-14): synthesis retries MUST write to the
        target out_path pinned in the payload. Previously the handler
        dispatched and threw result.text away — retries marked done
        with no file written. Synthesis is a file-producing job; the
        output IS the point. If the enqueue site didn't include an
        out_path (legacy payloads from before the fix), the retry
        just validates dispatch worked — degraded but not wrong."""
        try:
            from openclaw_dispatch import dispatch
            result = dispatch(
                agent=payload.get("agent", "jenna"),
                message=payload.get("prompt", ""),
                thinking=payload.get("thinking", "low"),
                timeout=payload.get("timeout", 120),
            )
            if not result.ok:
                return False
            # Write the synthesis output to the pinned path if provided
            out_path = payload.get("out_path")
            if out_path and result.text:
                try:
                    p = Path(out_path)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    # Atomic: tmp + rename
                    tmp = p.with_suffix(p.suffix + ".tmp")
                    tmp.write_text(result.text, encoding="utf-8")
                    tmp.rename(p)
                except Exception as e:
                    log.warning("handle_synthesis out_path write failed: %s", e)
                    return False
            return True
        except Exception as e:
            log.debug("handle_synthesis failed: %s", e)
            return False

    # ── proactive ───────────────────────────────────────────
    def _handle_proactive(payload: dict) -> bool:
        try:
            from proactive import run_proactive_sweep
            run_proactive_sweep()
            return True
        except Exception as e:
            log.debug("handle_proactive failed: %s", e)
            return False

    # ── telegram ────────────────────────────────────────────
    def _handle_telegram(payload: dict) -> bool:
        """MR7 fix (2026-04-14): accept all severity taxonomies used by
        callers (slos.py uses 'critical'/'warning', brain_loop uses
        'urgent'/'warn', etc.) so delayed alerts don't lose their
        urgency hint by falling through to the generic default.
        """
        try:
            from openclaw_dispatch import dispatch
            body = payload.get("body", "")
            severity = (payload.get("severity", "info") or "info").lower()
            # Normalize severity aliases
            if severity in ("critical", "urgent"):
                normalized = "urgent"
                prefix = "[DELAYED URGENT] "
            elif severity in ("warning", "warn", "error"):
                normalized = "warn"
                prefix = "[DELAYED WARN] "
            else:
                normalized = "info"
                prefix = "[DELAYED] "
            result = dispatch(
                agent="jenna",
                message=f"{prefix}{body}",
                thinking="off",
                timeout=60,
            )
            return bool(result.ok)
        except Exception as e:
            log.debug("handle_telegram failed: %s", e)
            return False

    # ── reflect ─────────────────────────────────────────────
    def _handle_reflect(payload: dict) -> bool:
        try:
            from openclaw_dispatch import dispatch
            result = dispatch(
                agent="sage",
                message=payload.get("prompt", ""),
                thinking="medium",
                timeout=payload.get("timeout", 120),
            )
            return bool(result.ok)
        except Exception as e:
            log.debug("handle_reflect failed: %s", e)
            return False

    _handlers.update({
        "classify":  _handle_classify,
        "entities":  _handle_entities,
        "distill":   _handle_distill,
        "synthesis": _handle_synthesis,
        "proactive": _handle_proactive,
        "telegram":  _handle_telegram,
        "reflect":   _handle_reflect,
    })


# ── Drain ─────────────────────────────────────────────────────────

def _breaker_open() -> bool:
    """Return True if llm.dispatch breaker is open — means LLM is still
    unavailable and we should not waste drain attempts."""
    try:
        from breakers import peek_breaker
        return peek_breaker("llm.dispatch").is_open
    except Exception:
        return False


def _ttl_cutoff(kind: str) -> str:
    ttl = KIND_TTL_SECONDS.get(kind, 7 * 24 * 3600)
    cutoff = datetime.now(timezone.utc).timestamp() - ttl
    return datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat(timespec="seconds")


def drain(limit: int = DEFAULT_DRAIN_LIMIT, abort_on_breaker: bool = True) -> dict:
    """Process up to `limit` pending entries. Returns stats dict."""
    t0 = time.time()
    _wire_default_handlers()

    if abort_on_breaker and _breaker_open():
        return {
            "status": "skipped_breaker",
            "drained": 0,
            "failed": 0,
            "abandoned": 0,
            "latency_ms": int((time.time() - t0) * 1000),
        }

    drained = 0
    failed = 0
    abandoned = 0

    try:
        with _connect() as conn:
            # Abandon entries past TTL first
            for kind in VALID_KINDS:
                cutoff = _ttl_cutoff(kind)
                abandon_rows = conn.execute(
                    "UPDATE llm_backlog SET status='abandoned' "
                    "WHERE status='pending' AND kind=? AND created_at < ?",
                    (kind, cutoff),
                )
                abandoned += abandon_rows.rowcount or 0

            # Pull pending entries — order by created_at ASC so oldest wins
            rows = conn.execute(
                "SELECT id, kind, payload_json, retry_count FROM llm_backlog "
                "WHERE status='pending' ORDER BY created_at ASC LIMIT ?",
                (limit,),
            ).fetchall()

            for row in rows:
                if abort_on_breaker and _breaker_open():
                    break  # quota died mid-drain, stop wasting attempts
                rid = int(row["id"])
                kind = row["kind"]
                try:
                    payload = json.loads(row["payload_json"])
                except json.JSONDecodeError:
                    conn.execute(
                        "UPDATE llm_backlog SET status='failed', "
                        "last_error='invalid payload_json' WHERE id=?",
                        (rid,),
                    )
                    failed += 1
                    continue

                handler = _handlers.get(kind)
                if handler is None:
                    conn.execute(
                        "UPDATE llm_backlog SET status='failed', "
                        "last_error='no handler' WHERE id=?",
                        (rid,),
                    )
                    failed += 1
                    continue

                try:
                    ok = bool(handler(payload))
                except Exception as e:
                    ok = False
                    err_text = str(e)[:200]
                else:
                    err_text = ""

                if ok:
                    conn.execute(
                        "UPDATE llm_backlog SET status='done', "
                        "last_attempt_at=?, retry_count=retry_count+1 WHERE id=?",
                        (_now_iso(), rid),
                    )
                    drained += 1
                else:
                    new_retry = int(row["retry_count"]) + 1
                    if new_retry >= MAX_RETRIES:
                        conn.execute(
                            "UPDATE llm_backlog SET status='failed', "
                            "last_attempt_at=?, retry_count=?, last_error=? WHERE id=?",
                            (_now_iso(), new_retry, err_text or "max retries", rid),
                        )
                        failed += 1
                    else:
                        conn.execute(
                            "UPDATE llm_backlog SET "
                            "last_attempt_at=?, retry_count=?, last_error=? WHERE id=?",
                            (_now_iso(), new_retry, err_text, rid),
                        )
    except sqlite3.Error as e:
        log.warning("llm_backlog.drain sqlite error: %s", e)

    return {
        "status": "ok",
        "drained": drained,
        "failed": failed,
        "abandoned": abandoned,
        "latency_ms": int((time.time() - t0) * 1000),
    }


# ── Stats + SLO helpers ──────────────────────────────────────────

def pending_count() -> int:
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM llm_backlog WHERE status='pending'"
            ).fetchone()
            return int(row["c"]) if row else 0
    except sqlite3.Error:
        return 0


def oldest_pending_age_seconds() -> float:
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT created_at FROM llm_backlog WHERE status='pending' "
                "ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            if not row:
                return 0.0
            dt = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    except (sqlite3.Error, ValueError):
        return 0.0


def stats() -> dict:
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT kind, status, COUNT(*) AS c FROM llm_backlog "
                "GROUP BY kind, status"
            ).fetchall()
            out: dict[str, dict[str, int]] = {}
            for r in rows:
                out.setdefault(r["kind"], {})[r["status"]] = int(r["c"])
            out["_totals"] = {
                "pending": pending_count(),
                "oldest_age_s": int(oldest_pending_age_seconds()),
            }
            return out
    except sqlite3.Error:
        return {}


# ── Scheduler entry point ────────────────────────────────────────

def run() -> dict:
    """Cron entry point. Returns JSON-serializable dict."""
    result = drain()
    result["pending_after"] = pending_count()
    result["oldest_age_s"] = int(oldest_pending_age_seconds())
    return result


if __name__ == "__main__":
    print(json.dumps(run()))
