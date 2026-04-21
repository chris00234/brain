#!/opt/homebrew/bin/python3
"""brain_core/fts_index.py — SQLite FTS5 keyword search fallback.

Rebuilds nightly from Qdrant via the VectorStore abstraction. Works as a
fallback when Qdrant is down. Portable — the entire search index is a single
SQLite file.

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
    "semantic_memory",
    "knowledge",
    "canonical",
    "experience",
    "obsidian",
    "personal",
    "notes",
    "messages",
    "calendar",
    "tasks",
]

# FTS5 reserved operators — stripped from user queries to prevent syntax errors
_FTS5_RESERVED = re.compile(r"\b(AND|OR|NOT|NEAR)\b", re.IGNORECASE)
_FTS5_SPECIAL = re.compile(r'["\(\)\*\^\:]')


def _get_conn():
    conn = sqlite3.connect(str(FTS_DB))
    conn.row_factory = sqlite3.Row
    # Disable Python's implicit-transaction mode so explicit BEGIN/COMMIT works.
    # Without this, `conn.execute("BEGIN")` after any DML raises
    # "cannot start a transaction within a transaction". Bug fix 2026-04-12.
    conn.isolation_level = None
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


def rebuild_from_vector_store():
    """Full rebuild of FTS index from Qdrant. Called nightly.

    Uses a shadow-table swap so search remains available during the rebuild:
      1. Create `memories_new` virtual table
      2. Populate it from Qdrant via VectorStore.get (paginated scroll)
      3. Atomically: drop old, rename new → memories
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from vector_store import get_vector_store

    ensure_schema()
    store = get_vector_store()
    available = set(store.list_collections())

    conn = _get_conn()
    total = 0
    PAGE = 1000
    try:
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
            if col_name not in available:
                continue
            count = 0
            offset = 0
            while True:
                try:
                    pts = store.get(
                        col_name,
                        limit=PAGE,
                        offset=offset,
                        with_payload=True,
                        with_documents=True,
                        with_vectors=False,
                    )
                except Exception as e:
                    print(f"  {col_name}: fetch failed at offset={offset}: {e}")
                    break
                if not pts:
                    break
                for p in pts:
                    meta = p.payload or {}
                    conn.execute(
                        "INSERT INTO memories_new (id, collection, content, title, path) VALUES (?, ?, ?, ?, ?)",
                        (
                            p.id,
                            col_name,
                            p.document or "",
                            meta.get("title", ""),
                            meta.get("source", meta.get("path", "")),
                        ),
                    )
                    count += 1
                    total += 1
                if len(pts) < PAGE:
                    break
                offset += PAGE
            print(f"  {col_name}: indexed {count} docs")

        conn.execute("BEGIN")
        conn.execute("DROP TABLE IF EXISTS memories_old")
        conn.execute("ALTER TABLE memories RENAME TO memories_old")
        conn.execute("ALTER TABLE memories_new RENAME TO memories")
        conn.commit()
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
                (safe_query, collection, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, collection, content, title, path, rank FROM memories "
                "WHERE memories MATCH ? ORDER BY rank LIMIT ?",
                (safe_query, limit),
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
    rebuild_from_vector_store()
