"""tests/unit/test_ingest_mirror_supersession.py — semantic supersession gate.

The 2026-04-26 change replaced the blunt "expire all older same-topic atoms"
SQL UPDATE with a per-candidate cosine similarity check. Restatements stay
valid; only real contradictions get valid_until set.

These tests stub get_embedding so the unit suite stays offline.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from brain_core import ingest_mirror


def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "brain.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            """
            CREATE TABLE atoms (
                id TEXT PRIMARY KEY,
                chroma_id TEXT NOT NULL UNIQUE,
                text TEXT NOT NULL,
                tier TEXT NOT NULL DEFAULT 'episodic',
                topic_key TEXT,
                speaker_entity TEXT NOT NULL DEFAULT 'chris',
                valid_until TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.executemany(
            "INSERT INTO atoms (id, chroma_id, text, topic_key, speaker_entity, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, 'chris', ?, ?)",
            [
                (
                    "atm_old1",
                    "ch_old1",
                    "Chris speaks Korean fluently.",
                    "languages",
                    "2026-04-01",
                    "2026-04-01",
                ),
                (
                    "atm_old2",
                    "ch_old2",
                    "Chris uses ChromaDB as the vector store.",
                    "languages",
                    "2026-04-02",
                    "2026-04-02",
                ),
                (
                    "atm_new",
                    "ch_new",
                    "Chris communicates comfortably in Korean.",
                    "languages",
                    "2026-04-26",
                    "2026-04-26",
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return db


@pytest.fixture()
def patched_supersession(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = _make_db(tmp_path)

    import atoms_store

    def _fake_conn(_path: Path | None = None):
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            c = sqlite3.connect(str(db))
            c.row_factory = sqlite3.Row
            try:
                yield c
            finally:
                c.close()

        return _cm()

    monkeypatch.setattr(atoms_store, "_conn", _fake_conn)

    import indexer

    def _fake_embedding(text: str, _retries: int = 5, use_cache: bool = True, prefix: str = "passage"):
        t = text.lower()
        if "chromadb" in t or "vector store" in t:
            return [0.0, 1.0, 0.0]
        if "korean" in t:
            return [1.0, 0.0, 0.0]
        return [0.5, 0.5, 0.0]

    monkeypatch.setattr(indexer, "get_embedding", _fake_embedding)
    return db


def _read_valid_untils(db: Path) -> dict[str, str | None]:
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute("SELECT id, valid_until FROM atoms").fetchall()
    finally:
        conn.close()
    return {row[0]: row[1] for row in rows}


def test_restatement_keeps_older_atoms_valid(patched_supersession: Path) -> None:
    result = ingest_mirror.MirrorResult(atom_id="atm_new")
    ingest_mirror._run_semantic_supersession(
        content="Chris communicates comfortably in Korean.",
        chroma_id="ch_new",
        topic_key="languages",
        speaker_entity="chris",
        now_iso="2026-04-26T00:00:00Z",
        result=result,
    )
    valid_untils = _read_valid_untils(patched_supersession)
    assert valid_untils["atm_old1"] is None, "Korean restatement should not expire the older Korean atom"


def test_real_contradiction_expires_older_atom(patched_supersession: Path) -> None:
    result = ingest_mirror.MirrorResult(atom_id="atm_new")
    ingest_mirror._run_semantic_supersession(
        content="Chris communicates comfortably in Korean.",
        chroma_id="ch_new",
        topic_key="languages",
        speaker_entity="chris",
        now_iso="2026-04-26T00:00:00Z",
        result=result,
    )
    valid_untils = _read_valid_untils(patched_supersession)
    assert (
        valid_untils["atm_old2"] == "2026-04-26T00:00:00Z"
    ), "ChromaDB-vs-Korean orthogonal atom should be marked expired"


def test_supersede_skips_when_no_candidates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "brain.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE atoms (id TEXT PRIMARY KEY, chroma_id TEXT NOT NULL UNIQUE, text TEXT NOT NULL, "
            "tier TEXT NOT NULL DEFAULT 'episodic', topic_key TEXT, speaker_entity TEXT NOT NULL DEFAULT 'chris', "
            "valid_until TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.commit()
    finally:
        conn.close()

    import atoms_store

    def _fake_conn(_path: Path | None = None):
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            c = sqlite3.connect(str(db))
            c.row_factory = sqlite3.Row
            try:
                yield c
            finally:
                c.close()

        return _cm()

    monkeypatch.setattr(atoms_store, "_conn", _fake_conn)
    result = ingest_mirror.MirrorResult(atom_id="atm_new")
    ingest_mirror._run_semantic_supersession(
        content="anything",
        chroma_id="ch_new",
        topic_key="empty",
        speaker_entity="chris",
        now_iso="2026-04-26T00:00:00Z",
        result=result,
    )
    assert result.warnings == []
    assert result.superseded_topic is False


def test_thresholds_are_conservative() -> None:
    assert ingest_mirror.SUPERSEDE_REINFORCE_FLOOR > ingest_mirror.SUPERSEDE_EXPIRE_CEILING
    assert ingest_mirror.SUPERSEDE_REINFORCE_FLOOR - ingest_mirror.SUPERSEDE_EXPIRE_CEILING >= 0.10
