from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from open_loops import classify_open_loop_text, open_loop_snapshot, scan_atom_open_loops, scan_task_open_loops


def _bootstrap_atoms(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE atoms (
                id TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                kind TEXT DEFAULT 'fact',
                confidence REAL DEFAULT 0.7,
                tier TEXT DEFAULT 'episodic',
                superseded_by TEXT,
                valid_from TEXT DEFAULT '2026-05-01T00:00:00Z',
                created_at TEXT DEFAULT '2026-05-01T00:00:00Z',
                updated_at TEXT DEFAULT '2026-05-01T00:00:00Z'
            );
            """
        )


def _bootstrap_tasks(db: Path) -> None:
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                status TEXT NOT NULL,
                assigned_agent TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )


def test_classify_open_loop_accepts_durable_commitment() -> None:
    candidate = classify_open_loop_text(
        "I will follow up with Sam about the launch checklist by 2026-06-20.",
        source_id="atm_1",
        created_at="2026-06-01T00:00:00Z",
        now=datetime(2026, 6, 21, tzinfo=UTC),
    )

    assert candidate is not None
    assert candidate.kind == "follow_up"
    assert candidate.due_hint == "2026-06-20"
    assert candidate.stale is True
    assert candidate.confidence >= 0.8


def test_classify_open_loop_rejects_session_chatter() -> None:
    assert (
        classify_open_loop_text(
            "Maybe we could follow up someday if the idea still feels useful.",
            source_id="atm_chatter",
            now=datetime(2026, 6, 21, tzinfo=UTC),
        )
        is None
    )


def test_classify_open_loop_rejects_resolved_items() -> None:
    assert (
        classify_open_loop_text(
            "I will follow up with Sam about the launch checklist — completed and closed.",
            source_id="atm_done",
            now=datetime(2026, 6, 21, tzinfo=UTC),
        )
        is None
    )


def test_scan_atom_open_loops_filters_superseded_and_chatter(tmp_path: Path) -> None:
    db = tmp_path / "brain.db"
    _bootstrap_atoms(db)
    with sqlite3.connect(db) as conn:
        conn.executemany(
            "INSERT INTO atoms (id, text, tier, superseded_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    "atm_open",
                    "We need to follow up with Dana before 2026-06-15 about billing access.",
                    "episodic",
                    None,
                    "2026-06-01T00:00:00Z",
                    "2026-06-01T00:00:00Z",
                ),
                (
                    "atm_chatter",
                    "Maybe we could follow up with Dana if billing becomes important.",
                    "episodic",
                    None,
                    "2026-06-02T00:00:00Z",
                    "2026-06-02T00:00:00Z",
                ),
                (
                    "atm_old",
                    "I will ping Dana about billing access.",
                    "episodic",
                    "atm_open",
                    "2026-06-01T00:00:00Z",
                    "2026-06-01T00:00:00Z",
                ),
            ],
        )

    items = scan_atom_open_loops(
        brain_db_path=db,
        now=datetime(2026, 6, 21, tzinfo=UTC),
        stale_days=14,
    )

    assert [item["id"] for item in items] == ["atm_open"]
    assert items[0]["kind"] == "follow_up"
    assert items[0]["stale"] is True


def test_scan_task_open_loops_surfaces_stale_non_terminal_tasks(tmp_path: Path) -> None:
    db = tmp_path / "autonomy.db"
    _bootstrap_tasks(db)
    with sqlite3.connect(db) as conn:
        conn.executemany(
            "INSERT INTO tasks (id, title, description, status, assigned_agent, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "task_open",
                    "Waiting on vendor credentials",
                    "Need Chris to approve credential rotation.",
                    "pending",
                    "brain_cli",
                    "2026-06-01T00:00:00Z",
                    "2026-06-01T00:00:00Z",
                ),
                (
                    "task_done",
                    "Closed work",
                    "Finished",
                    "completed",
                    "brain_cli",
                    "2026-06-01T00:00:00Z",
                    "2026-06-01T00:00:00Z",
                ),
            ],
        )

    items = scan_task_open_loops(
        autonomy_db_path=db,
        now=datetime(2026, 6, 21, tzinfo=UTC),
        stale_days=14,
    )

    assert [item["id"] for item in items] == ["task_open"]
    assert items[0]["source"] == "task_queue"
    assert items[0]["stale"] is True


def test_open_loop_snapshot_combines_atoms_and_tasks(tmp_path: Path) -> None:
    brain_db = tmp_path / "brain.db"
    autonomy_db = tmp_path / "autonomy.db"
    _bootstrap_atoms(brain_db)
    _bootstrap_tasks(autonomy_db)
    with sqlite3.connect(brain_db) as conn:
        conn.execute(
            "INSERT INTO atoms (id, text, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (
                "atm_waiting",
                "Waiting on Mira to send the contract before 2026-06-10.",
                "2026-06-01T00:00:00Z",
                "2026-06-01T00:00:00Z",
            ),
        )
    with sqlite3.connect(autonomy_db) as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, description, status, assigned_agent, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "task_paused",
                "Paused integration",
                "Waiting for review.",
                "paused",
                "brain_cli",
                "2026-06-01T00:00:00Z",
                "2026-06-01T00:00:00Z",
            ),
        )

    snapshot = open_loop_snapshot(
        brain_db_path=brain_db,
        autonomy_db_path=autonomy_db,
        now=datetime(2026, 6, 21, tzinfo=UTC),
    )

    assert snapshot["total"] == 2
    assert snapshot["stale_count"] == 2
    assert snapshot["detector"]["rejects_session_chatter"] is True
    assert {item["source"] for item in snapshot["items"]} == {"atom", "task_queue"}
