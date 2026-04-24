from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "cli"))

import brain_stale_cli


def test_iter_candidates_can_audit_conjectures(tmp_path):
    db = tmp_path / "brain.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE atoms (
                id TEXT,
                text TEXT,
                kind TEXT,
                tier TEXT,
                created_at TEXT,
                last_reviewed_at TEXT,
                reinforcement_count INTEGER,
                confidence REAL,
                provenance_json TEXT,
                superseded_by TEXT
            )
            """
        )
        old = (datetime.now(UTC) - timedelta(days=45)).isoformat(timespec="seconds")
        conn.execute(
            """
            INSERT INTO atoms
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "dream_conjecture_old",
                "Dream conjecture should be reviewed after it stays unreinforced.",
                "conjecture",
                "episodic",
                old,
                None,
                0,
                0.3,
                "{}",
                None,
            ),
        )

    with patch.object(brain_stale_cli, "BRAIN_DB", db):
        candidates = list(brain_stale_cli._iter_candidates("conjecture", None))

    assert len(candidates) == 1
    assert candidates[0]["id"] == "dream_conjecture_old"
    assert candidates[0]["decay_days"] == 30
