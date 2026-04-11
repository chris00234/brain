"""Structured fact store — (entity, attribute, value) triples with temporal validity.

Provides a normalized layer above raw semantic_memory for precise fact retrieval.
Facts are deduplicated on (entity, attribute) — newer values supersede older ones.

Usage:
    from fact_store import store_fact, query_facts, get_entity_facts

    store_fact("chris cho", "location", "Irvine, California",
               source="canonical/_profile.md", confidence=0.95)

    facts = query_facts(entity="chris cho")
    facts = query_facts(attribute="preferred_framework")
    facts = get_entity_facts("chris cho")
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    from config import BRAIN_LOGS_DIR
except ImportError:
    BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")

DB_PATH = BRAIN_LOGS_DIR / "facts.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id TEXT PRIMARY KEY,
    entity TEXT NOT NULL,
    attribute TEXT NOT NULL,
    value TEXT NOT NULL,
    normalized_value TEXT,
    valid_from TEXT,
    valid_to TEXT,
    observed_at TEXT NOT NULL,
    source TEXT,
    source_type TEXT,
    confidence REAL DEFAULT 0.5,
    status TEXT DEFAULT 'active',
    supersedes TEXT,
    superseded_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fact_entity ON facts(entity);
CREATE INDEX IF NOT EXISTS idx_fact_attribute ON facts(attribute);
CREATE INDEX IF NOT EXISTS idx_fact_entity_attr ON facts(entity, attribute);
CREATE INDEX IF NOT EXISTS idx_fact_status ON facts(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fact_entity_attr_value ON facts(entity, attribute, normalized_value);
"""


_schema_initialized = False
_schema_lock = threading.Lock()


def _ensure_schema():
    global _schema_initialized
    if _schema_initialized:
        return
    with _schema_lock:
        if _schema_initialized:
            return
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        conn.close()
        _schema_initialized = True


@contextlib.contextmanager
def _conn_ctx():
    """Short-lived connection — avoids thread-local leaks in worker pools."""
    _ensure_schema()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize(value: str) -> str:
    """Normalize a value for dedup comparison."""
    import re
    return re.sub(r'[^a-z0-9]+', '_', value.lower()).strip('_')


def store_fact(
    entity: str,
    attribute: str,
    value: str,
    source: str = "",
    source_type: str = "",
    confidence: float = 0.5,
    valid_from: str = "",
    valid_to: str = "",
    observed_at: str = "",
) -> dict:
    """Store a fact triple. Deduplicates on (entity, attribute, normalized_value).

    If a fact with the same (entity, attribute) but different value exists:
    - If temporal: keeps both (different valid_from/valid_to)
    - If same time: supersedes old value with newer one (higher confidence wins)
    """
    now = _now()
    normalized = _normalize(value)
    fact_id = f"fact_{uuid.uuid4().hex[:12]}"
    action = "created"
    with _conn_ctx() as conn:
        existing = conn.execute(
            "SELECT * FROM facts WHERE entity = ? AND attribute = ? AND status = 'active' "
            "ORDER BY confidence DESC, updated_at DESC",
            (entity.lower(), attribute.lower()),
        ).fetchall()

        for ex in existing:
            if _normalize(ex["value"]) == normalized:
                conn.execute(
                    "UPDATE facts SET confidence = MAX(confidence, ?), "
                    "observed_at = ?, updated_at = ?, source = COALESCE(NULLIF(?, ''), source) "
                    "WHERE id = ?",
                    (confidence, observed_at or now, now, source, ex["id"]),
                )
                conn.commit()
                return {"status": "updated", "id": ex["id"], "action": "confidence_bump"}

        supersede_ids = []
        for ex in existing:
            if ex["valid_to"] and valid_from:
                continue
            if confidence >= (ex["confidence"] or 0):
                supersede_ids.append(ex["id"])
                action = "superseded"
            else:
                fact_id_low = f"fact_{uuid.uuid4().hex[:12]}"
                conn.execute(
                    "INSERT OR IGNORE INTO facts "
                    "(id, entity, attribute, value, normalized_value, valid_from, valid_to, "
                    " observed_at, source, source_type, confidence, status, supersedes, superseded_by, "
                    " created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'superseded', '', ?, ?, ?)",
                    (fact_id_low, entity.lower(), attribute.lower(), value, normalized,
                     valid_from or now, now, observed_at or now,
                     source, source_type, confidence, ex["id"], now, now),
                )
                conn.commit()
                return {"status": "superseded_by_existing", "id": fact_id_low}

        try:
            for sid in supersede_ids:
                conn.execute(
                    "UPDATE facts SET status = 'superseded', superseded_by = ?, "
                    "valid_to = ?, updated_at = ? WHERE id = ?",
                    (fact_id, now, now, sid),
                )
            conn.execute(
                "INSERT INTO facts "
                "(id, entity, attribute, value, normalized_value, valid_from, valid_to, "
                " observed_at, source, source_type, confidence, status, supersedes, "
                " created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                (
                    fact_id, entity.lower(), attribute.lower(), value, normalized,
                    valid_from or now, valid_to, observed_at or now,
                    source, source_type, confidence,
                    ",".join(supersede_ids), now, now,
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            return {"status": "duplicate", "action": "skipped"}

    # Audit trail
    try:
        from audit_log import log_event
        log_event(
            event_type="fact_store",
            entity_a=entity,
            entity_b=f"{attribute}={value}",
            match_score=confidence,
            resolution=action,
            reason=f"Fact stored: {entity}.{attribute} = {value}",
        )
    except Exception:
        pass

    return {"status": action, "id": fact_id}


def query_facts(
    entity: str | None = None,
    attribute: str | None = None,
    active_only: bool = True,
    limit: int = 50,
) -> list[dict]:
    """Query facts with optional filters."""
    clauses = []
    params: list = []
    if entity:
        clauses.append("entity = ?")
        params.append(entity.lower())
    if attribute:
        clauses.append("attribute = ?")
        params.append(attribute.lower())
    if active_only:
        clauses.append("status = 'active'")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _conn_ctx() as conn:
        rows = conn.execute(
            f"SELECT * FROM facts {where} ORDER BY confidence DESC, updated_at DESC LIMIT ?",
            params + [limit],
        ).fetchall()
    return [dict(r) for r in rows]


def get_entity_facts(entity: str) -> list[dict]:
    """Get all active facts for an entity."""
    return query_facts(entity=entity, active_only=True, limit=100)


def get_fact_history(entity: str, attribute: str) -> list[dict]:
    """Get all versions of a fact (active + superseded) for temporal tracing."""
    return query_facts(entity=entity, attribute=attribute, active_only=False, limit=50)


def stats() -> dict:
    """Return fact store summary stats."""
    with _conn_ctx() as conn:
        total = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM facts WHERE status = 'active'").fetchone()[0]
        entities = conn.execute("SELECT COUNT(DISTINCT entity) FROM facts").fetchone()[0]
        attributes = conn.execute("SELECT COUNT(DISTINCT attribute) FROM facts").fetchone()[0]
    return {
        "total_facts": total,
        "active_facts": active,
        "superseded_facts": total - active,
        "unique_entities": entities,
        "unique_attributes": attributes,
    }
