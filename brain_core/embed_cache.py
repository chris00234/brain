"""Shared SQLite embedding cache for indexer and search modules.

Replaces two separate caches (embedding_cache.db and embed_cache.db)
with one shared cache. WAL mode enables concurrent reads.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from pathlib import Path

log = logging.getLogger("brain.embed_cache")

try:
    from config import EMBED_CACHE_DB, EMBED_MODEL
except ImportError:
    EMBED_CACHE_DB = Path("/Users/chrischo/server/brain/logs/embedding_cache.db")
    EMBED_MODEL = "blaifa/multilingual-e5-large-instruct"

_lock = threading.Lock()
_local = threading.local()

# Hit/miss counters for observability (exposed via /metrics)
_stats_lock = threading.Lock()
_cache_hits = 0
_cache_misses = 0
_put_counter = 0
_CHECKPOINT_EVERY = 500


def cache_stats() -> dict:
    """Return cache hit/miss stats."""
    with _stats_lock:
        h, m = _cache_hits, _cache_misses
    total = h + m
    return {
        "hits": h,
        "misses": m,
        "total": total,
        "hit_rate": round(h / total, 3) if total > 0 else 0.0,
    }


def _get_conn() -> sqlite3.Connection:
    conn = getattr(_local, 'conn', None)
    if conn is None:
        EMBED_CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(EMBED_CACHE_DB), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        conn.execute("PRAGMA cache_size=-16000")
        conn.execute("CREATE TABLE IF NOT EXISTS embeddings (hash TEXT PRIMARY KEY, embedding BLOB)")
        # Additive migration: add created_at column for TTL-based eviction
        try:
            conn.execute("ALTER TABLE embeddings ADD COLUMN created_at TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # column already exists
        _local.conn = conn
    return conn


def text_hash(text: str, max_chars: int = 1200) -> str:
    """Model-scoped cache key. Prepending EMBED_MODEL prevents stale vectors
    from a prior model being returned after an embed-model swap (which would
    cause dimension mismatches and silent recall regressions)."""
    scoped = f"{EMBED_MODEL}:{text[:max_chars]}"
    return hashlib.md5(scoped.encode()).hexdigest()


def cache_get(key: str) -> list[float] | None:
    global _cache_hits, _cache_misses
    try:
        conn = _get_conn()
        cur = conn.execute("SELECT embedding FROM embeddings WHERE hash = ?", (key,))
        row = cur.fetchone()
        if row:
            with _stats_lock:
                _cache_hits += 1
            return json.loads(row[0])
    except Exception as e:
        log.warning("embed_cache.cache_get failed: %s", e)
    with _stats_lock:
        _cache_misses += 1
    return None


def cache_put(key: str, embedding: list[float]) -> None:
    global _put_counter
    try:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn = _get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO embeddings (hash, embedding, created_at) VALUES (?, ?, ?)",
            (key, json.dumps(embedding), now),
        )
        conn.commit()
        with _stats_lock:
            _put_counter += 1
            should_checkpoint = _put_counter % _CHECKPOINT_EVERY == 0
        if should_checkpoint:
            try:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except Exception:
                pass
    except Exception:
        pass


def prune_old(max_age_days: int = 60, max_rows: int = 25_000) -> dict:
    """Two-tier cleanup: legacy + age-based + size-cap LRU + VACUUM.

    1. Legacy rows (created_at IS NULL OR ''): added before the `created_at`
       column existed. Oldest by definition — delete all.
    2. Age-based: delete rows with created_at < now - max_age_days.
    3. Size cap: if still > max_rows, delete oldest by created_at ASC.
    4. VACUUM to reclaim disk pages (outside the transaction).

    Zero quality risk: a deleted entry becomes a cache miss on next query
    and re-embeds via Ollama (~65ms). Pruned entries will rarely be
    re-queried — that's why they aged out.

    Returns dict with counts + disk size before/after.
    """
    from datetime import datetime, timezone, timedelta
    result = {
        "legacy_deleted": 0, "aged_deleted": 0, "lru_deleted": 0,
        "rows_before": 0, "rows_after": 0,
        "bytes_before": 0, "bytes_after": 0,
    }
    try:
        conn = _get_conn()
        result["bytes_before"] = EMBED_CACHE_DB.stat().st_size if EMBED_CACHE_DB.exists() else 0
        result["rows_before"] = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]

        with _lock:
            cur = conn.execute("DELETE FROM embeddings WHERE created_at IS NULL OR created_at = ''")
            result["legacy_deleted"] = cur.rowcount

            cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat(timespec="seconds")
            cur = conn.execute("DELETE FROM embeddings WHERE created_at < ?", (cutoff,))
            result["aged_deleted"] = cur.rowcount

            remaining = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
            if remaining > max_rows:
                excess = remaining - max_rows
                cur = conn.execute(
                    "DELETE FROM embeddings WHERE hash IN "
                    "(SELECT hash FROM embeddings ORDER BY created_at ASC LIMIT ?)",
                    (excess,),
                )
                result["lru_deleted"] = cur.rowcount

            conn.commit()

        # VACUUM outside the data lock — needs exclusive DB lock and can't
        # run inside a transaction. WAL checkpoint first to fold pending
        # writes into the main DB so VACUUM sees them.
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
        except Exception as e:
            log.warning("embed_cache.prune_old VACUUM failed (non-fatal): %s", e)

        result["rows_after"] = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        result["bytes_after"] = EMBED_CACHE_DB.stat().st_size if EMBED_CACHE_DB.exists() else 0

        log.info(
            "embed_cache.prune_old: legacy=%d aged=%d lru=%d rows=%d->%d size=%.1fMB->%.1fMB",
            result["legacy_deleted"], result["aged_deleted"], result["lru_deleted"],
            result["rows_before"], result["rows_after"],
            result["bytes_before"] / 1048576, result["bytes_after"] / 1048576,
        )
    except Exception as e:
        log.warning("embed_cache.prune_old failed: %s", e)
        result["error"] = str(e)[:200]
    return result


if __name__ == "__main__":
    # One-shot runner so cron/scheduler can invoke without importing:
    #   python3 /Users/chrischo/server/brain/brain_core/embed_cache.py
    import argparse
    parser = argparse.ArgumentParser(description="Prune embed_cache.db")
    parser.add_argument("--max-age-days", type=int, default=60)
    parser.add_argument("--max-rows", type=int, default=25_000)
    args = parser.parse_args()
    out = prune_old(max_age_days=args.max_age_days, max_rows=args.max_rows)
    print(json.dumps(out, indent=2))
