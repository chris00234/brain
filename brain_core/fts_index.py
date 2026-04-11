#!/opt/homebrew/bin/python3
"""brain_core/fts_index.py — SQLite FTS5 keyword search fallback.

Rebuilds nightly from ChromaDB. Works as a fallback when ChromaDB is down.
Portable — the entire search index is a single SQLite file.

Tokenizer: `unicode61 remove_diacritics 2` — handles CJK (Korean) + English
without English-only stemming. The porter stemmer mangles Korean tokens.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

FTS_DB = Path("/Users/chrischo/server/brain/logs/fts_index.db")

# Includes both pre- and post-migration personal collection names, so FTS
# search works regardless of whether migrate_personal.py has been run yet.
MONITORED_COLLECTIONS = [
    "semantic_memory", "knowledge", "canonical", "experience",
    "obsidian", "personal", "notes", "messages", "calendar", "tasks",
]

# FTS5 reserved operators — stripped from user queries to prevent syntax errors
_FTS5_RESERVED = re.compile(r"\b(AND|OR|NOT|NEAR)\b", re.IGNORECASE)
_FTS5_SPECIAL = re.compile(r'["\(\)\*\^\:]')


def _get_conn():
    conn = sqlite3.connect(str(FTS_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure_schema():
    FTS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = _get_conn()
    try:
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memories USING fts5(
                id UNINDEXED,
                collection UNINDEXED,
                content,
                title,
                path,
                tokenize='unicode61 remove_diacritics 2'
            )
        """)
        conn.commit()
    finally:
        conn.close()


def rebuild_from_chroma():
    """Full rebuild of FTS index from ChromaDB. Called nightly.

    Uses a shadow-table swap so search remains available during the rebuild:
      1. Create `memories_new` virtual table
      2. Populate it from ChromaDB
      3. Atomically: drop old, rename new → memories
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from http_pool import http_json
    from search import get_collections

    ensure_schema()
    cols = get_collections()

    conn = _get_conn()
    total = 0
    try:
        # Drop any stale shadow from a previous failed run
        conn.execute("DROP TABLE IF EXISTS memories_new")
        conn.execute("""
            CREATE VIRTUAL TABLE memories_new USING fts5(
                id UNINDEXED,
                collection UNINDEXED,
                content,
                title,
                path,
                tokenize='unicode61 remove_diacritics 2'
            )
        """)

        for col_name in MONITORED_COLLECTIONS:
            col_id = cols.get(col_name)
            if not col_id:
                continue
            try:
                resp = http_json("POST",
                    f"http://127.0.0.1:8000/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/get",
                    {"limit": 50000, "include": ["documents", "metadatas"]})
            except Exception as e:
                print(f"  {col_name}: fetch failed: {e}")
                continue

            ids = resp.get("ids", [])
            docs = resp.get("documents", []) or []
            metas = resp.get("metadatas", []) or []

            for i, doc, meta in zip(ids, docs, metas):
                meta = meta or {}
                conn.execute(
                    "INSERT INTO memories_new (id, collection, content, title, path) VALUES (?, ?, ?, ?, ?)",
                    (i, col_name, doc or "", meta.get("title", ""), meta.get("source", meta.get("path", "")))
                )
                total += 1

            print(f"  {col_name}: indexed {len(ids)} docs")

        # Atomic swap — FTS5 rename is supported. Wrap in an explicit transaction.
        conn.execute("BEGIN")
        conn.execute("DROP TABLE IF EXISTS memories_old")
        conn.execute("ALTER TABLE memories RENAME TO memories_old")
        conn.execute("ALTER TABLE memories_new RENAME TO memories")
        conn.commit()
        # Clean up old table outside transaction
        conn.execute("DROP TABLE IF EXISTS memories_old")
        conn.commit()
    finally:
        conn.close()

    print(f"FTS5 rebuilt: {total} total docs")
    return total


def _sanitize_query(query: str) -> str:
    """Strip FTS5 reserved operators and special chars; keep alphanumeric + CJK + whitespace."""
    # Remove reserved operators (AND/OR/NOT/NEAR)
    q = _FTS5_RESERVED.sub("", query)
    # Remove special chars that break FTS5 syntax
    q = _FTS5_SPECIAL.sub("", q)
    # Collapse whitespace, strip leading/trailing
    q = " ".join(q.split())
    return q.strip()


def search_fts(query: str, limit: int = 10, collection: str | None = None) -> list[dict]:
    """BM25 keyword search via FTS5. Handles Korean + English via unicode61 tokenizer."""
    ensure_schema()
    safe_query = _sanitize_query(query)
    if not safe_query:
        return []

    conn = _get_conn()
    try:
        if collection:
            rows = conn.execute(
                "SELECT id, collection, content, title, path, rank FROM memories "
                "WHERE memories MATCH ? AND collection = ? ORDER BY rank LIMIT ?",
                (safe_query, collection, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, collection, content, title, path, rank FROM memories "
                "WHERE memories MATCH ? ORDER BY rank LIMIT ?",
                (safe_query, limit)
            ).fetchall()

        return [
            {
                "id": r["id"],
                "collection": r["collection"],
                "content": (r["content"] or "")[:500],
                "title": r["title"] or "",
                # RRF keys on `path` — fall back to id when path is empty so
                # different docs can't collide into one anonymous bucket.
                "path": r["path"] or r["id"] or "",
                "source": r["path"] or r["id"] or "",
                "source_type": "fts",
                "score": -float(r["rank"]) * 50,  # FTS5 rank is negative, normalize
                "trust_tier": 2,
                "metadata": {"collection": r["collection"]},
            }
            for r in rows
        ]
    finally:
        conn.close()


if __name__ == "__main__":
    # If the existing schema uses the old porter tokenizer, force a fresh rebuild
    # by dropping the table. New schema uses unicode61 which handles CJK.
    try:
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='memories'"
            ).fetchone()
            if row and "porter" in (row[0] or ""):
                print("Detected legacy porter tokenizer — dropping for rebuild")
                conn.execute("DROP TABLE memories")
                conn.commit()
        finally:
            conn.close()
    except Exception:
        pass
    rebuild_from_chroma()
