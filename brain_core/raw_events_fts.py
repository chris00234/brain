"""brain_core/raw_events_fts.py — live FTS5 index over raw_events.

Complements fts_index.py (which syncs from ChromaDB nightly). This module
queries an FTS5 virtual table built directly on top of brain.db's raw_events
table, kept in sync via triggers. Targets the extended eval 64% gap:
literal-wording queries that fail semantic retrieval because raw event streams
(sessions, git commits, browser history) are excluded by default from the
ChromaDB fan-out (search_unified._search_rag line 765).

Schema created in brain.db (idempotent — ok if already present):
  CREATE VIRTUAL TABLE raw_events_fts USING fts5(
      content, source_type, actor,
      content='raw_events', content_rowid='rowid',
      tokenize='unicode61 remove_diacritics 2'
  );

  + AFTER INSERT/UPDATE/DELETE triggers on raw_events to keep in sync.

Usage:
    from raw_events_fts import search
    hits = search("BRAIN_ADAPTIVE_RAG", limit=5)
    # → [{"id", "content", "source_type", "actor", "timestamp", "score", ...}]

The tokenizer (unicode61 remove_diacritics 2) handles both English + Korean
without porter stemming mangling Korean syllables.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

log = logging.getLogger("brain.raw_events_fts")

BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")

# FTS5 reserved syntax that would cause parse errors if the user types it
_FTS5_RESERVED = re.compile(r"\b(AND|OR|NOT|NEAR)\b", re.IGNORECASE)
_FTS5_SPECIAL = re.compile(r'["\(\)\*\^\:]')


def _sanitize(query: str) -> str:
    """Prep a user query for FTS5 MATCH. Returns empty string if unsafe."""
    if not query:
        return ""
    cleaned = _FTS5_SPECIAL.sub(" ", query).strip()
    tokens = []
    for tok in cleaned.split():
        if not tok:
            continue
        # Quote hyphenated or identifier-like tokens to treat as phrases.
        # `BRAIN_ADAPTIVE_RAG` → `"BRAIN_ADAPTIVE_RAG"` to avoid column-syntax parse.
        if re.search(r"[-_:/\\.]", tok) or len(tok) > 40:
            tokens.append(f'"{tok}"')
        else:
            # Also drop bare reserved words to avoid malformed MATCH
            if _FTS5_RESERVED.fullmatch(tok):
                continue
            tokens.append(tok)
    return " ".join(tokens)[:400]


def search(query: str, limit: int = 10) -> list[dict]:
    """Live FTS5 search over raw_events. Returns ranked hits or [] on error."""
    safe = _sanitize(query)
    if not safe:
        return []
    if not BRAIN_DB.exists():
        return []
    try:
        conn = sqlite3.connect(str(BRAIN_DB))
        conn.row_factory = sqlite3.Row
        try:
            # Check that the FTS table exists before querying. If missing, return
            # empty rather than error — lets brain-server come up on a fresh db.
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='raw_events_fts'"
            ).fetchone()
            if not exists:
                return []
            rows = conn.execute(
                """
                SELECT re.id AS id,
                       substr(re.content, 1, 500) AS content,
                       re.source_type AS source_type,
                       re.actor AS actor,
                       re.timestamp AS timestamp,
                       rank AS fts_rank
                FROM raw_events_fts
                JOIN raw_events re ON raw_events_fts.rowid = re.rowid
                WHERE raw_events_fts MATCH ?
                ORDER BY rank LIMIT ?
                """,
                (safe, limit),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        # Malformed FTS5 expression reaches here despite sanitization on some
        # edge-case unicode. Fail-open.
        log.debug("raw_events_fts query failed: %s — query=%r", exc, safe)
        return []
    except Exception as exc:
        log.debug("raw_events_fts unexpected error: %s", exc)
        return []

    # Normalize to the shape search_unified expects
    results = []
    for r in rows:
        content = r["content"] or ""
        src_type = r["source_type"] or "raw"
        results.append(
            {
                "id": r["id"],
                "content": content,
                "title": f"{src_type}: {content[:80]}",
                "source": f"raw_events:{r['id']}",
                "source_type": "raw_events_fts",
                "raw_source_type": src_type,
                "actor": r["actor"] or "",
                "timestamp": r["timestamp"] or "",
                # fts_rank is negative (lower = better). Mirror fts_index.py's scaling.
                "score": -float(r["fts_rank"]) * 50,
                "trust_tier": 1,  # raw events = lower trust than curated canonical (tier 3)
                "metadata": {
                    "source_type": src_type,
                    "actor": r["actor"] or "",
                    "timestamp": r["timestamp"] or "",
                    "collection": "raw_events_fts",
                },
            }
        )
    return results


def ensure_schema() -> dict:
    """Idempotent: create the FTS5 virtual table + triggers on brain.db.

    Returns {created: bool, rows: int} for observability. Callable at server
    boot so a fresh brain.db self-initializes.
    """
    summary = {"created": False, "rows": 0, "error": None}
    try:
        conn = sqlite3.connect(str(BRAIN_DB))
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='raw_events_fts'"
            ).fetchone()
            if not exists:
                conn.executescript(
                    """
                    CREATE VIRTUAL TABLE raw_events_fts USING fts5(
                        content, source_type, actor,
                        content='raw_events', content_rowid='rowid',
                        tokenize='unicode61 remove_diacritics 2'
                    );
                    CREATE TRIGGER IF NOT EXISTS raw_events_ai AFTER INSERT ON raw_events BEGIN
                        INSERT INTO raw_events_fts(rowid, content, source_type, actor)
                        VALUES (new.rowid, new.content, new.source_type, new.actor);
                    END;
                    CREATE TRIGGER IF NOT EXISTS raw_events_ad AFTER DELETE ON raw_events BEGIN
                        INSERT INTO raw_events_fts(raw_events_fts, rowid, content, source_type, actor)
                        VALUES ('delete', old.rowid, old.content, old.source_type, old.actor);
                    END;
                    CREATE TRIGGER IF NOT EXISTS raw_events_au AFTER UPDATE ON raw_events BEGIN
                        INSERT INTO raw_events_fts(raw_events_fts, rowid, content, source_type, actor)
                        VALUES ('delete', old.rowid, old.content, old.source_type, old.actor);
                        INSERT INTO raw_events_fts(rowid, content, source_type, actor)
                        VALUES (new.rowid, new.content, new.source_type, new.actor);
                    END;
                    """
                )
                conn.execute("INSERT INTO raw_events_fts(raw_events_fts) VALUES('rebuild')")
                conn.commit()
                summary["created"] = True
            n = conn.execute("SELECT count(*) FROM raw_events_fts").fetchone()[0]
            summary["rows"] = n
        finally:
            conn.close()
    except Exception as exc:
        summary["error"] = str(exc)[:200]
    return summary


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "schema":
        print(json.dumps(ensure_schema(), indent=2))
    else:
        q = sys.argv[1] if len(sys.argv) > 1 else "BRAIN_ADAPTIVE_RAG"
        hits = search(q, limit=5)
        print(json.dumps(hits, indent=2, ensure_ascii=False))
