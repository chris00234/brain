"""tests/unit/test_recall_structural_judge.py — deterministic recall scoring.

Spins up an in-memory action_audit + atoms pair, runs the structural
judge, and asserts: (a) bands map correctly, (b) atom_id and chroma_id
both resolve, (c) structural labels are written to the sidecar table while
action_audit.outcome stays NULL for the LLM judge.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "brain_core"))

import recall_structural_judge as rsj  # noqa: E402


def _bootstrap_brain_db(path: Path) -> None:
    conn = sqlite3.connect(path)
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
        CREATE TABLE atoms (
            id TEXT PRIMARY KEY,
            text TEXT,
            confidence REAL,
            updated_at TEXT,
            chroma_id TEXT
        );
        """
    )
    conn.commit()
    conn.close()


def _insert_atom(path: Path, atom_id: str, text: str, confidence: float, chroma_id: str = "") -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO atoms (id, text, confidence, updated_at, chroma_id) VALUES (?, ?, ?, datetime('now'), ?)",
        (atom_id, text, confidence, chroma_id),
    )
    conn.commit()
    conn.close()


def _insert_audit(
    path: Path,
    *,
    query: str,
    atom_ids: list[str] | None = None,
    chroma_ids: list[str] | None = None,
    actor: str = "codex",
    created_at: str | None = None,
) -> int:
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO action_audit (route, actor, query_text, retrieved_atom_ids, retrieved_chroma_ids, created_at) "
        "VALUES ('/recall/v2', ?, ?, ?, ?, ?)",
        (
            actor,
            query,
            json.dumps(atom_ids or []),
            json.dumps(chroma_ids or []),
            created_at or datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        ),
    )
    rid = cur.lastrowid
    conn.commit()
    conn.close()
    return rid


def _audit_outcome(path: Path, rid: int) -> str | None:
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT outcome FROM action_audit WHERE id=?", (rid,)).fetchone()[0]


def _structural_outcome(path: Path, rid: int) -> str | None:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT outcome FROM recall_structural_judgments WHERE action_audit_id=?",
            (rid,),
        ).fetchone()
    return row[0] if row else None


def test_structural_score_labels_relevant_recall_as_good(tmp_path: Path) -> None:
    db = tmp_path / "brain.db"
    _bootstrap_brain_db(db)
    _insert_atom(db, "atm_1", "Brain self quality goal subtask scaffold deterministic", 0.9)
    rid = _insert_audit(db, query="brain self quality scaffold deterministic", atom_ids=["atm_1"])
    counters = rsj.run(hours=1, brain_db_path=db)
    assert counters["status"] == "ok"
    assert counters["labeled_good"] == 1
    assert _audit_outcome(db, rid) is None
    assert _structural_outcome(db, rid) == "structural_good"


def test_structural_score_resolves_chroma_id(tmp_path: Path) -> None:
    db = tmp_path / "brain.db"
    _bootstrap_brain_db(db)
    _insert_atom(
        db,
        "atm_2",
        "Recall structural judge deterministic scoring band threshold",
        0.85,
        chroma_id="chroma-uuid-2",
    )
    rid = _insert_audit(
        db,
        query="recall structural judge band scoring deterministic",
        chroma_ids=["chroma-uuid-2"],
    )
    counters = rsj.run(hours=1, brain_db_path=db)
    assert counters["labeled_good"] == 1
    assert _audit_outcome(db, rid) is None
    assert _structural_outcome(db, rid) == "structural_good"


def test_neutral_band_leaves_outcome_null(tmp_path: Path) -> None:
    db = tmp_path / "brain.db"
    _bootstrap_brain_db(db)
    # Vague doc + mild overlap → neutral band → action_audit stays NULL, but
    # the sidecar records neutral so the row is not rescored every hour.
    _insert_atom(db, "atm_3", "system reminder notice flag generic placeholder", 0.5)
    rid = _insert_audit(db, query="brain quality override pattern detector", atom_ids=["atm_3"])
    counters = rsj.run(hours=1, brain_db_path=db)
    assert counters["labeled_neutral"] >= 0
    assert counters["labeled_good"] == 0
    assert counters["labeled_wrong"] == 0
    assert _audit_outcome(db, rid) is None
    assert _structural_outcome(db, rid) == "structural_neutral"


def test_skipped_when_no_atom_or_chroma_ids(tmp_path: Path) -> None:
    db = tmp_path / "brain.db"
    _bootstrap_brain_db(db)
    _insert_audit(db, query="query with empty refs", atom_ids=[], chroma_ids=[])
    counters = rsj.run(hours=1, brain_db_path=db)
    assert counters["skipped_empty"] == 1


def test_structural_judge_skips_shell_hook_noise(tmp_path: Path) -> None:
    db = tmp_path / "brain.db"
    _bootstrap_brain_db(db)
    _insert_atom(db, "atm_noise", "grep command output should not become recall quality", 0.9)
    rid = _insert_audit(
        db,
        query="grep -R TODO /Users/chrischo/server/brain",
        atom_ids=["atm_noise"],
        actor="unknown",
    )

    counters = rsj.run(hours=1, brain_db_path=db)

    assert counters["scanned"] == 0
    assert _audit_outcome(db, rid) is None


def test_structural_judge_honors_iso_hour_window(tmp_path: Path) -> None:
    db = tmp_path / "brain.db"
    _bootstrap_brain_db(db)
    _insert_atom(db, "atm_old", "brain quality scaffold old row should remain unlabeled", 0.9)
    old_same_day = (
        (datetime.now(UTC) - timedelta(hours=3)).isoformat(timespec="seconds").replace("+00:00", "Z")
    )
    rid = _insert_audit(
        db,
        query="brain quality scaffold old row",
        atom_ids=["atm_old"],
        created_at=old_same_day,
    )

    counters = rsj.run(hours=1, brain_db_path=db)

    assert counters["scanned"] == 0
    assert _audit_outcome(db, rid) is None


def test_structural_judge_does_not_rescore_sidecar_rows(tmp_path: Path) -> None:
    db = tmp_path / "brain.db"
    _bootstrap_brain_db(db)
    _insert_atom(db, "atm_1", "Brain self quality goal subtask scaffold deterministic", 0.9)
    rid = _insert_audit(db, query="brain self quality scaffold deterministic", atom_ids=["atm_1"])

    first = rsj.run(hours=1, brain_db_path=db)
    second = rsj.run(hours=1, brain_db_path=db)

    assert first["labeled_good"] == 1
    assert second["scanned"] == 0
    assert _audit_outcome(db, rid) is None
    assert _structural_outcome(db, rid) == "structural_good"
