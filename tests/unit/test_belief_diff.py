"""tests/unit/test_belief_diff.py — belief_diff window math + shape."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from brain_core import belief_diff


def _iso(offset_days: float) -> str:
    return (datetime.now(UTC) + timedelta(days=offset_days)).strftime("%Y-%m-%dT%H:%M:%S+00:00")


@pytest.fixture()
def seeded_dbs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    brain_db = tmp_path / "brain.db"
    autonomy_db = tmp_path / "autonomy.db"

    conn = sqlite3.connect(str(brain_db))
    try:
        conn.execute(
            """
            CREATE TABLE atoms (
                id TEXT PRIMARY KEY, text TEXT, kind TEXT DEFAULT 'fact',
                confidence REAL DEFAULT 0.5, tier TEXT DEFAULT 'episodic',
                canonical INTEGER DEFAULT 0, supersedes TEXT, superseded_by TEXT,
                trust_score REAL DEFAULT 0.5, valid_from TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                chroma_id TEXT UNIQUE
            )
            """
        )
        # Old canonical (out of window) — excluded
        conn.execute(
            "INSERT INTO atoms (id, text, tier, canonical, valid_from, created_at, updated_at, chroma_id) "
            "VALUES ('atm_old_canon', 'old fact', 'semantic', 1, ?, ?, ?, 'c1')",
            (_iso(-100), _iso(-100), _iso(-100)),
        )
        # Recent canonical (in window) — included
        conn.execute(
            "INSERT INTO atoms (id, text, tier, canonical, valid_from, created_at, updated_at, chroma_id) "
            "VALUES ('atm_new_canon', 'new fact', 'semantic', 1, ?, ?, ?, 'c2')",
            (_iso(-2), _iso(-3), _iso(-1)),
        )
        # Replacement edge in window
        conn.execute(
            "INSERT INTO atoms (id, text, tier, canonical, valid_from, created_at, updated_at, chroma_id) "
            "VALUES ('atm_replaced', 'old version', 'episodic', 0, ?, ?, ?, 'c3')",
            (_iso(-10), _iso(-10), _iso(-10)),
        )
        conn.execute(
            "INSERT INTO atoms (id, text, tier, canonical, supersedes, valid_from, created_at, updated_at, chroma_id) "
            "VALUES ('atm_replacement', 'new version', 'episodic', 0, 'atm_replaced', ?, ?, ?, 'c4')",
            (_iso(-1), _iso(-1), _iso(-1)),
        )
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(str(autonomy_db))
    try:
        conn.execute(
            """
            CREATE TABLE decision_ledger (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, actor TEXT DEFAULT 'brain',
                domain TEXT DEFAULT 'general', source TEXT DEFAULT '', observation_kind TEXT DEFAULT '',
                observation_subject TEXT DEFAULT '', perceived_state_json TEXT DEFAULT '{}',
                candidate_options_json TEXT DEFAULT '[]', selected_option TEXT DEFAULT '',
                selected_payload_json TEXT DEFAULT '{}', confidence REAL DEFAULT 0.0,
                autonomy_level TEXT DEFAULT '', expected_outcome TEXT DEFAULT '',
                actual_outcome TEXT DEFAULT '', outcome_status TEXT DEFAULT 'pending',
                review_status TEXT DEFAULT 'unreviewed', action_audit_id INTEGER, resolved_at TEXT
            )
            """
        )
        # Failed within window
        conn.execute(
            "INSERT INTO decision_ledger (id, created_at, outcome_status, resolved_at, observation_subject) "
            "VALUES ('d1', ?, 'failed', ?, 'infra:rollback')",
            (
                _iso(-3),
                _iso(-2),
            ),
        )
        # Old failure (out of window) — excluded
        conn.execute(
            "INSERT INTO decision_ledger (id, created_at, outcome_status, resolved_at, observation_subject) "
            "VALUES ('d_old', ?, 'failed', ?, 'old:thing')",
            (_iso(-30), _iso(-29)),
        )
        # Succeeded — excluded
        conn.execute(
            "INSERT INTO decision_ledger (id, created_at, outcome_status, resolved_at, observation_subject) "
            "VALUES ('d_ok', ?, 'succeeded', ?, 'good:thing')",
            (_iso(-1), _iso(-1)),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(belief_diff, "BRAIN_DB", brain_db)
    monkeypatch.setattr(belief_diff, "AUTONOMY_DB", autonomy_db)
    return brain_db, autonomy_db


def test_compute_diff_returns_in_window_changes(seeded_dbs: tuple[Path, Path]) -> None:
    out = belief_diff.compute_diff(since_days=7, limit=50)
    canonical_ids = {a["id"] for a in out["newly_canonical"]}
    assert "atm_new_canon" in canonical_ids
    assert "atm_old_canon" not in canonical_ids

    sup_pairs = {(s["new_atom_id"], s["replaced_atom_id"]) for s in out["new_supersessions"]}
    assert ("atm_replacement", "atm_replaced") in sup_pairs

    decision_ids = {d["id"] for d in out["reversed_decisions"]}
    assert "d1" in decision_ids
    assert "d_old" not in decision_ids
    assert "d_ok" not in decision_ids

    assert out["totals"]["newly_canonical"] >= 1
    assert out["totals"]["reversed_decisions"] == 1


def test_compute_diff_clamps_args(seeded_dbs: tuple[Path, Path]) -> None:
    out = belief_diff.compute_diff(since_days=999, limit=999)
    assert out["since_days"] == 90  # clamped
    assert out["limit"] == 500


def test_compute_diff_dbs_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(belief_diff, "BRAIN_DB", tmp_path / "missing_brain.db")
    monkeypatch.setattr(belief_diff, "AUTONOMY_DB", tmp_path / "missing_autonomy.db")
    out = belief_diff.compute_diff(since_days=7)
    assert out["newly_canonical"] == []
    assert out["new_supersessions"] == []
    assert out["reversed_decisions"] == []
    assert out["tier_growth"] == {}
