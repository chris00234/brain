"""tests/unit/test_atom_deboost.py — outcome-aware weight updates."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "brain_core"))

import atom_deboost as ad  # noqa: E402


def _bootstrap(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE action_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            route TEXT NOT NULL,
            actor TEXT DEFAULT 'codex',
            query_text TEXT,
            retrieved_atom_ids TEXT,
            retrieved_chroma_ids TEXT,
            outcome TEXT,
            outcome_reason TEXT,
            resolved_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE recall_judgments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_audit_id INTEGER NOT NULL,
            query_text TEXT,
            relevance REAL,
            groundedness REAL,
            reason TEXT,
            judge_provider TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    conn.commit()
    conn.close()


def _add_judgment(db: Path, atoms: list[str], relevance: float) -> int:
    with sqlite3.connect(db) as conn:
        cur = conn.execute(
            "INSERT INTO action_audit (route, retrieved_atom_ids) VALUES ('/recall/v2', ?)",
            (json.dumps(atoms),),
        )
        audit_id = cur.lastrowid
        conn.execute(
            "INSERT INTO recall_judgments (action_audit_id, relevance) VALUES (?, ?)",
            (audit_id, relevance),
        )
    return audit_id


def test_update_weights_penalizes_repeat_wrong_atoms(tmp_path: Path) -> None:
    db = tmp_path / "brain.db"
    _bootstrap(db)
    # atm_bad shows up in 3 wrong-judged recalls
    for _ in range(3):
        _add_judgment(db, ["atm_bad"], relevance=0.1)
    summary = ad.update_weights(brain_db_path=db)
    assert summary["status"] == "ok"
    with sqlite3.connect(db) as conn:
        weight = conn.execute("SELECT weight FROM atom_deboost WHERE atom_id='atm_bad'").fetchone()[0]
    # 1.0 - 3*0.15 = 0.55
    assert weight < 1.0
    assert abs(weight - 0.55) < 0.01


def test_update_weights_recovers_right_atoms(tmp_path: Path) -> None:
    db = tmp_path / "brain.db"
    _bootstrap(db)
    # Pre-seed an atom at 0.40
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE atom_deboost (atom_id TEXT PRIMARY KEY, weight REAL, "
            "evidence_json TEXT, reason TEXT, updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO atom_deboost VALUES ('atm_redeemed', 0.40, '{}', 'prior', '2026-05-01T00:00:00Z')"
        )
    # Now 4 right-judged appearances → +0.40 → caps at 1.0 not 0.80
    for _ in range(4):
        _add_judgment(db, ["atm_redeemed"], relevance=0.9)
    summary = ad.update_weights(brain_db_path=db)
    assert summary["status"] == "ok"
    with sqlite3.connect(db) as conn:
        weight = conn.execute("SELECT weight FROM atom_deboost WHERE atom_id='atm_redeemed'").fetchone()[0]
    # 0.40 + 4*0.10 = 0.80
    assert abs(weight - 0.80) < 0.01


def test_load_weight_map_only_below_floor(tmp_path: Path) -> None:
    db = tmp_path / "brain.db"
    _bootstrap(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE atom_deboost (atom_id TEXT PRIMARY KEY, weight REAL, "
            "evidence_json TEXT, reason TEXT, updated_at TEXT)"
        )
        conn.executemany(
            "INSERT INTO atom_deboost VALUES (?, ?, '{}', 'seed', '2026-05-01T00:00:00Z')",
            [("atm_low", 0.10), ("atm_mid", 0.50), ("atm_high", 0.95)],
        )
    m = ad.load_weight_map(brain_db_path=db, floor=0.20)
    assert m == {"atm_low": 0.10}
    m2 = ad.load_weight_map(brain_db_path=db, floor=0.60)
    assert set(m2.keys()) == {"atm_low", "atm_mid"}


def test_update_weights_db_missing(tmp_path: Path) -> None:
    summary = ad.update_weights(brain_db_path=tmp_path / "missing.db")
    assert summary["status"] == "db_missing"


def test_load_weight_map_db_missing(tmp_path: Path) -> None:
    m = ad.load_weight_map(brain_db_path=tmp_path / "missing.db")
    assert m == {}
