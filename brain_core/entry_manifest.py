"""SQLite manifest for Brain v2 source documents and vector chunks.

This is the durable audit layer for the entry contract. Qdrant stores the
searchable payload; brain.db stores an append-friendly manifest that tells us
which source document produced which vector point under which chunk/tag policy.
All functions are best-effort and must never block ingestion.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

log = logging.getLogger("brain.entry_manifest")

try:
    from config import BRAIN_DB
except Exception:  # pragma: no cover - import fallback for standalone tools
    BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")

ENTRY_MANIFEST_DDL = """
CREATE TABLE IF NOT EXISTS entry_documents (
  document_id          TEXT PRIMARY KEY,
  source_kind          TEXT NOT NULL DEFAULT 'unknown',
  source_type          TEXT NOT NULL DEFAULT 'unknown',
  source_ref           TEXT NOT NULL DEFAULT '',
  source_name          TEXT NOT NULL DEFAULT '',
  document_title       TEXT NOT NULL DEFAULT '',
  content_hash         TEXT NOT NULL DEFAULT '',
  schema_version       TEXT NOT NULL DEFAULT 'brain-entry-v2',
  chunk_policy_version TEXT NOT NULL DEFAULT 'source-aware-v2',
  tag_policy_version   TEXT NOT NULL DEFAULT 'normalized-tags-v1',
  chunk_strategy       TEXT NOT NULL DEFAULT 'paragraph',
  tags_json            TEXT NOT NULL DEFAULT '[]',
  metadata_json        TEXT NOT NULL DEFAULT '{}',
  first_seen_at        TEXT NOT NULL,
  last_indexed_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entry_documents_source ON entry_documents(source_type, source_kind);
CREATE INDEX IF NOT EXISTS idx_entry_documents_hash ON entry_documents(content_hash);
CREATE INDEX IF NOT EXISTS idx_entry_documents_strategy ON entry_documents(chunk_strategy);

CREATE TABLE IF NOT EXISTS entry_chunks (
  vector_id            TEXT NOT NULL,
  document_id          TEXT NOT NULL,
  collection           TEXT NOT NULL,
  chunk_id             TEXT NOT NULL DEFAULT '',
  chunk_index          INTEGER,
  chunk_count          INTEGER,
  content_hash         TEXT NOT NULL DEFAULT '',
  chunk_strategy       TEXT NOT NULL DEFAULT 'paragraph',
  is_parent            INTEGER NOT NULL DEFAULT 0,
  parent_chunk_id      TEXT NOT NULL DEFAULT '',
  tags_json            TEXT NOT NULL DEFAULT '[]',
  metadata_json        TEXT NOT NULL DEFAULT '{}',
  indexed_at           TEXT NOT NULL,
  FOREIGN KEY(document_id) REFERENCES entry_documents(document_id) ON DELETE CASCADE,
  PRIMARY KEY(collection, vector_id)
);
CREATE INDEX IF NOT EXISTS idx_entry_chunks_document ON entry_chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_entry_chunks_collection ON entry_chunks(collection);
CREATE INDEX IF NOT EXISTS idx_entry_chunks_strategy ON entry_chunks(chunk_strategy);
CREATE INDEX IF NOT EXISTS idx_entry_chunks_parent ON entry_chunks(parent_chunk_id) WHERE parent_chunk_id != '';
"""

_HOT_METADATA_KEYS = {
    "agent",
    "category",
    "chunk_count",
    "chunk_id",
    "chunk_index",
    "chunk_policy_version",
    "chunk_strategy",
    "chunk_version",
    "collection",
    "content_hash",
    "document_id",
    "document_section",
    "document_title",
    "document_type",
    "entry_schema_version",
    "is_parent",
    "parent_id",
    "schema_version",
    "scope",
    "semantic_chunk_candidate",
    "source",
    "source_document_id",
    "source_kind",
    "source_name",
    "source_path",
    "source_type",
    "speaker_entity",
    "tag_policy_version",
    "tags",
    "topic_key",
    "type",
    "vector_collection",
}


def _now() -> str:
    """Z-suffix UTC timestamp. Delegates to db.now_iso(z_suffix=True) so
    entry_documents.last_indexed_at lex-sorts with atoms_store / entity_graph
    timestamps without divergence."""
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent))
    from db import now_iso as _db_now_iso

    return _db_now_iso(z_suffix=True)


def ensure_entry_manifest_schema(db_path: Path | None = None) -> None:
    path = db_path or BRAIN_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path), timeout=30) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(ENTRY_MANIFEST_DDL)
        conn.commit()


def _as_json(value: Any, default: str) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return default


def _int_or_none(value: Any) -> int | None:
    try:
        if value in (None, "", [], {}):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _document_id(payload: dict[str, Any], document: str) -> str:
    doc_id = payload.get("document_id") or payload.get("source_document_id")
    if doc_id:
        return str(doc_id)
    try:
        from document_provenance import document_id_for_source

        return document_id_for_source(
            str(payload.get("source") or payload.get("source_path") or payload.get("path") or ""),
            title=str(
                payload.get("document_title") or payload.get("title") or payload.get("source_name") or ""
            ),
            content=document,
        )
    except Exception:
        from hashlib import sha256

        digest = sha256(document[:500].encode("utf-8", errors="ignore")).hexdigest()[:16]
        return f"doc:unknown:{digest}"


def _metadata_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: payload[key] for key in sorted(_HOT_METADATA_KEYS) if key in payload}


def record_vector_entries(
    *,
    collection: str,
    ids: Sequence[str],
    payloads: Sequence[dict[str, Any]],
    documents: Sequence[str] | None = None,
    db_path: Path | None = None,
) -> None:
    """Best-effort manifest write for a vector upsert batch."""

    if not ids or not payloads:
        return
    docs = list(documents or [""] * len(ids))
    now = _now()
    try:
        ensure_entry_manifest_schema(db_path)
        with sqlite3.connect(str(db_path or BRAIN_DB), timeout=30) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            for vector_id, payload, document in zip(ids, payloads, docs, strict=False):
                payload = dict(payload or {})
                document_id = _document_id(payload, document or "")
                tags_json = _as_json(payload.get("tags") or [], "[]")
                metadata_json = _as_json(_metadata_summary(payload), "{}")
                source_ref = str(
                    payload.get("source_path")
                    or payload.get("source")
                    or payload.get("path")
                    or payload.get("source_ref")
                    or ""
                )
                conn.execute(
                    """
                    INSERT INTO entry_documents (
                      document_id, source_kind, source_type, source_ref, source_name,
                      document_title, content_hash, schema_version, chunk_policy_version,
                      tag_policy_version, chunk_strategy, tags_json, metadata_json,
                      first_seen_at, last_indexed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(document_id) DO UPDATE SET
                      source_kind=excluded.source_kind,
                      source_type=excluded.source_type,
                      source_ref=excluded.source_ref,
                      source_name=excluded.source_name,
                      document_title=excluded.document_title,
                      content_hash=excluded.content_hash,
                      schema_version=excluded.schema_version,
                      chunk_policy_version=excluded.chunk_policy_version,
                      tag_policy_version=excluded.tag_policy_version,
                      chunk_strategy=excluded.chunk_strategy,
                      tags_json=excluded.tags_json,
                      metadata_json=excluded.metadata_json,
                      last_indexed_at=excluded.last_indexed_at
                    """,
                    (
                        document_id,
                        str(payload.get("source_kind") or "unknown"),
                        str(payload.get("source_type") or "unknown"),
                        source_ref,
                        str(payload.get("source_name") or ""),
                        str(payload.get("document_title") or payload.get("title") or ""),
                        str(payload.get("content_hash") or ""),
                        str(payload.get("schema_version") or "brain-entry-v2"),
                        str(
                            payload.get("chunk_policy_version")
                            or payload.get("chunk_version")
                            or "source-aware-v2"
                        ),
                        str(payload.get("tag_policy_version") or "normalized-tags-v1"),
                        str(payload.get("chunk_strategy") or "paragraph"),
                        tags_json,
                        metadata_json,
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO entry_chunks (
                      vector_id, document_id, collection, chunk_id, chunk_index,
                      chunk_count, content_hash, chunk_strategy, is_parent,
                      parent_chunk_id, tags_json, metadata_json, indexed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(collection, vector_id) DO UPDATE SET
                      document_id=excluded.document_id,
                      collection=excluded.collection,
                      chunk_id=excluded.chunk_id,
                      chunk_index=excluded.chunk_index,
                      chunk_count=excluded.chunk_count,
                      content_hash=excluded.content_hash,
                      chunk_strategy=excluded.chunk_strategy,
                      is_parent=excluded.is_parent,
                      parent_chunk_id=excluded.parent_chunk_id,
                      tags_json=excluded.tags_json,
                      metadata_json=excluded.metadata_json,
                      indexed_at=excluded.indexed_at
                    """,
                    (
                        str(vector_id),
                        document_id,
                        collection,
                        str(payload.get("chunk_id") or ""),
                        _int_or_none(payload.get("chunk_index")),
                        _int_or_none(payload.get("chunk_count")),
                        str(payload.get("content_hash") or ""),
                        str(payload.get("chunk_strategy") or "paragraph"),
                        1 if payload.get("is_parent") else 0,
                        str(payload.get("parent_id") or payload.get("parent_chunk_id") or ""),
                        tags_json,
                        metadata_json,
                        now,
                    ),
                )
            conn.commit()
    except Exception as exc:  # pragma: no cover - ingestion must stay fail-open
        log.debug("entry manifest write skipped: %s", exc)
