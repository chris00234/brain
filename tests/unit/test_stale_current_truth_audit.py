from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from brain_core.stale_current_truth import (
    build_atoms_report,
    build_report,
    find_current_truth_blockers_in_text,
)


def _write_note(path: Path, *, status: str = "active", body: str, superseded_by: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "id": path.stem,
        "type": "canonical",
        "status": status,
        "superseded_by": superseded_by,
    }
    path.write_text("---json\n" + json.dumps(metadata) + "\n---\n" + body)


def test_flags_active_current_chromadb_claim(tmp_path: Path) -> None:
    knowledge = tmp_path / "knowledge"
    _write_note(
        knowledge / "canonical" / "infra" / "rag.md",
        body="ChromaDB is the vector database service in the current Brain RAG stack.",
    )

    report = build_report(knowledge)

    assert not report["passed"]
    assert report["blocker_count"] == 1
    assert report["blockers"][0]["replaced_by"] == "Qdrant"


def test_allows_historical_chromadb_mentions(tmp_path: Path) -> None:
    knowledge = tmp_path / "knowledge"
    _write_note(
        knowledge / "canonical" / "infra" / "rag.md",
        body="ChromaDB is historical only; it was superseded by Qdrant on 2026-04-21.",
    )

    report = build_report(knowledge)

    assert report["passed"]
    assert report["historical_mentions_allowed"] == 1


def test_allows_decommissioned_era_over_mentions(tmp_path: Path) -> None:
    knowledge = tmp_path / "knowledge"
    _write_note(
        knowledge / "canonical" / "weekly" / "2026-W17.md",
        body="Qdrant is the retrieval substrate for the stack and the ChromaDB era is effectively over.",
    )

    report = build_report(knowledge)

    assert report["passed"]
    assert report["historical_mentions_allowed"] == 1


def test_skips_superseded_and_archived_notes(tmp_path: Path) -> None:
    knowledge = tmp_path / "knowledge"
    _write_note(
        knowledge / "canonical" / "entities" / "chromadb.md",
        status="superseded",
        superseded_by="entity_qdrant",
        body="ChromaDB is the vector database service in the current Brain RAG stack.",
    )
    _write_note(
        knowledge / "canonical" / "archived" / "old.md",
        body="ChromaDB is the vector database service in the current Brain RAG stack.",
    )

    report = build_report(knowledge)

    assert report["passed"]
    assert report["files_scanned"] == 1
    assert report["skipped_archived"] == 1


def test_loads_decommissioned_terms_from_config(tmp_path: Path) -> None:
    knowledge = tmp_path / "knowledge"
    config = tmp_path / "terms.json"
    config.write_text(
        json.dumps(
            [
                {
                    "term": "OldVector",
                    "replaced_by": "NewVector",
                    "decommissioned_at": "2026-04-24",
                    "current_doc": "canonical/infra/new-vector.md",
                    "aliases": ["OldVector"],
                }
            ]
        )
    )
    _write_note(
        knowledge / "canonical" / "infra" / "retrieval.md",
        body="OldVector is the current vector database service for retrieval.",
    )

    report = build_report(knowledge, config_path=config)

    assert not report["passed"]
    assert report["blockers"][0]["term"] == "OldVector"
    assert report["blockers"][0]["replaced_by"] == "NewVector"


def test_text_blocker_detector_supports_retrieval_filter() -> None:
    blockers = find_current_truth_blockers_in_text(
        "ChromaDB is the current vector database service for the Brain RAG stack.",
        source="semantic_memory:test",
    )

    assert blockers
    assert blockers[0]["term"] == "ChromaDB"


def test_text_blocker_detector_allows_history() -> None:
    blockers = find_current_truth_blockers_in_text(
        "ChromaDB was historical only and was replaced by Qdrant on 2026-04-21.",
        source="semantic_memory:test",
    )

    assert blockers == []


def _write_atoms_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE atoms (
          id TEXT PRIMARY KEY,
          chroma_id TEXT NOT NULL,
          text TEXT NOT NULL,
          kind TEXT NOT NULL,
          tier TEXT NOT NULL,
          confidence REAL,
          trust_score REAL,
          superseded_by TEXT,
          valid_until TEXT,
          updated_at TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO atoms
        (id, chroma_id, text, kind, tier, confidence, trust_score, superseded_by, valid_until, updated_at)
        VALUES
        ('a1', 'semantic_memory:a1', 'ChromaDB is the current vector database service.', 'fact', 'semantic', 0.9, 0.8, '', '', ''),
        ('a2', 'semantic_memory:a2', 'ChromaDB was replaced by Qdrant and is historical only.', 'fact', 'semantic', 0.9, 0.8, '', '', ''),
        ('a3', 'semantic_memory:a3', 'Dream conjecture: maybe use chroma_api as a recall gate.', 'conjecture', 'episodic', 0.3, 0.35, '', '', ''),
        ('a4', 'semantic_memory:a4', 'Already superseded.', 'fact', 'semantic', 0.8, 0.8, 'a5', '', '2026-04-24T00:00:00Z')
        """
    )
    conn.commit()
    conn.close()


def test_atoms_report_marks_active_current_claims_obsolete(tmp_path: Path) -> None:
    db_path = tmp_path / "brain.db"
    _write_atoms_db(db_path)

    report = build_atoms_report(db_path=db_path, apply=True, mirror_vector=False)

    assert not report["passed"]
    assert report["blocker_count"] == 2
    assert report["marked_atoms"] == 2
    assert report["superseded_valid_until_missing"] == 1
    assert report["repaired_superseded_valid_until"] == 1
    conn = sqlite3.connect(str(db_path))
    rows = dict(conn.execute("SELECT id, tier FROM atoms").fetchall())
    valid_until = conn.execute("SELECT valid_until FROM atoms WHERE id='a4'").fetchone()[0]
    conn.close()
    assert rows["a1"] == "obsolete"
    assert rows["a2"] == "semantic"
    assert rows["a3"] == "obsolete"
    assert valid_until == "2026-04-24T00:00:00Z"
