"""tests/unit/test_conflict_surfacer.py — conflict pairing + dedup."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "brain_core"))

import conflict_surfacer as cs  # noqa: E402


def _bootstrap(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE atoms (
            id TEXT PRIMARY KEY,
            text TEXT,
            kind TEXT DEFAULT 'fact',
            confidence REAL DEFAULT 0.5,
            tier TEXT DEFAULT 'episodic',
            superseded_by TEXT,
            trust_score REAL DEFAULT 0.5,
            topic_key TEXT,
            created_at TEXT DEFAULT '2026-05-14T00:00:00Z'
        );
        """
    )
    conn.commit()
    conn.close()


def _insert(db: Path, atom_id: str, text: str, topic_key: str, tier: str = "episodic") -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO atoms (id, text, tier, topic_key) VALUES (?, ?, ?, ?)",
            (atom_id, text, tier, topic_key),
        )


def test_find_conflicts_flags_polarity_flip(tmp_path: Path) -> None:
    db = tmp_path / "brain.db"
    _bootstrap(db)
    _insert(
        db,
        "atm_a",
        "The deploy pipeline is enabled and running smoothly on the main branch.",
        "decision:deploy_pipeline",
    )
    _insert(
        db,
        "atm_b",
        "The deploy pipeline is disabled and stopped after the main branch incident.",
        "decision:deploy_pipeline",
    )
    pairs = cs.find_conflicts(brain_db_path=db)
    assert len(pairs) == 1
    p = pairs[0]
    assert {p["atom_a"], p["atom_b"]} == {"atm_a", "atm_b"}
    assert "polarity" in p["reason"]


def test_find_conflicts_flags_numeric_divergence(tmp_path: Path) -> None:
    db = tmp_path / "brain.db"
    _bootstrap(db)
    _insert(
        db,
        "atm_a",
        "The backup runs at port 8791 every 30 minutes on the brain server.",
        "fact:backup_port",
    )
    _insert(
        db,
        "atm_b",
        "The backup runs at port 9999 every 30 minutes on the brain server.",
        "fact:backup_port",
    )
    pairs = cs.find_conflicts(brain_db_path=db)
    assert pairs
    assert "numeric" in pairs[0]["reason"]


def test_find_conflicts_skips_low_overlap(tmp_path: Path) -> None:
    db = tmp_path / "brain.db"
    _bootstrap(db)
    _insert(db, "atm_a", "Apples are red fruit grown in temperate climates.", "topic:x")
    _insert(db, "atm_b", "Submarines are not bicycles in any meaningful sense.", "topic:x")
    pairs = cs.find_conflicts(brain_db_path=db)
    assert pairs == []


def test_find_conflicts_skips_superseded(tmp_path: Path) -> None:
    db = tmp_path / "brain.db"
    _bootstrap(db)
    _insert(db, "atm_a", "The deploy pipeline is enabled and running.", "topic:y")
    _insert(db, "atm_b", "The deploy pipeline is disabled and stopped.", "topic:y")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE atoms SET superseded_by = 'atm_b' WHERE id = 'atm_a'")
    pairs = cs.find_conflicts(brain_db_path=db)
    assert pairs == []


def test_materialize_review_tasks_dedupes_against_open(tmp_path: Path) -> None:
    db = tmp_path / "brain.db"
    _bootstrap(db)
    _insert(db, "atm_a", "The deploy pipeline is enabled and running on main.", "topic:z")
    _insert(db, "atm_b", "The deploy pipeline is disabled and stopped on main.", "topic:z")

    sig = cs._signature("atm_a", "atm_b")

    class _FakeTQ:
        def __init__(self) -> None:
            self.created: list[dict] = []

        def list_tasks(self, status=None):
            return [{"metadata": {"conflict_signature": sig}}]

        def create_task(self, **kwargs):
            self.created.append(kwargs)
            return {"id": "task-1", "title": kwargs["title"]}

    tq = _FakeTQ()
    out = cs.materialize_review_tasks(brain_db_path=db, task_queue_obj=tq)
    assert out["created"] == []
    assert any(s["reason"] == "open_task_exists" for s in out["skipped"])
    assert tq.created == []  # did not actually create


def test_materialize_review_tasks_creates_when_no_dupes(tmp_path: Path) -> None:
    db = tmp_path / "brain.db"
    _bootstrap(db)
    _insert(db, "atm_a", "The deploy pipeline is enabled and running on main.", "topic:fresh")
    _insert(db, "atm_b", "The deploy pipeline is disabled and stopped on main.", "topic:fresh")

    class _FakeTQ:
        def __init__(self) -> None:
            self.created: list[dict] = []

        def list_tasks(self, status=None):
            return []

        def create_task(self, **kwargs):
            self.created.append(kwargs)
            return {"id": "task-99", "title": kwargs["title"]}

    tq = _FakeTQ()
    out = cs.materialize_review_tasks(brain_db_path=db, task_queue_obj=tq)
    assert len(out["created"]) == 1
    assert tq.created and tq.created[0]["assigned_agent"] == "brain_cli"
    assert tq.created[0]["metadata"]["conflict_signature"] == cs._signature("atm_a", "atm_b")
