from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core"))

from entry_manifest import ensure_entry_manifest_schema, record_vector_entries


def test_entry_manifest_records_documents_and_chunks(tmp_path):
    db_path = tmp_path / "brain.db"
    ensure_entry_manifest_schema(db_path)

    record_vector_entries(
        collection="knowledge",
        ids=["vec-1"],
        documents=["This is a document chunk."],
        payloads=[
            {
                "document_id": "doc:test:123",
                "source_kind": "file",
                "source_type": "obsidian_note",
                "source": "/tmp/test.md",
                "source_name": "test.md",
                "document_title": "Test",
                "content_hash": "abc",
                "schema_version": "brain-entry-v2",
                "chunk_policy_version": "source-aware-v2",
                "tag_policy_version": "normalized-tags-v1",
                "chunk_strategy": "semantic",
                "chunk_index": 0,
                "chunk_count": 1,
                "tags": ["note"],
            }
        ],
        db_path=db_path,
    )

    with sqlite3.connect(db_path) as conn:
        doc = conn.execute(
            "SELECT * FROM entry_documents WHERE document_id = ?", ("doc:test:123",)
        ).fetchone()
        chunk = conn.execute("SELECT * FROM entry_chunks WHERE vector_id = ?", ("vec-1",)).fetchone()

    assert doc is not None
    assert chunk is not None
