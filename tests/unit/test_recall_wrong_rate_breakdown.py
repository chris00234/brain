"""Regression tests for recall wrong-rate breakdown label sources."""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "brain_core"))

import recall_wrong_rate_breakdown as breakdown_mod  # noqa: E402


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _bootstrap(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE action_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                route TEXT NOT NULL,
                actor TEXT DEFAULT 'codex',
                query_text TEXT,
                outcome TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE recall_structural_judgments (
                action_audit_id INTEGER PRIMARY KEY,
                outcome TEXT NOT NULL,
                structural_score REAL NOT NULL,
                reason_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                judged_at TEXT NOT NULL
            );
            """
        )


def _insert_audit(path: Path, *, query: str, outcome: str | None = None, actor: str = "codex") -> int:
    with sqlite3.connect(path) as conn:
        cur = conn.execute(
            "INSERT INTO action_audit (route, actor, query_text, outcome, created_at) "
            "VALUES ('/recall/v2', ?, ?, ?, ?)",
            (actor, query, outcome, _now()),
        )
        return int(cur.lastrowid)


def _insert_structural(path: Path, action_id: int, outcome: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO recall_structural_judgments "
            "(action_audit_id, outcome, structural_score, reason_json, created_at, judged_at) "
            "VALUES (?, ?, 0.8, '{}', ?, ?)",
            (action_id, outcome, _now(), _now()),
        )


def test_breakdown_keeps_structural_sidecar_as_separate_label_source(tmp_path: Path) -> None:
    db = tmp_path / "brain.db"
    _bootstrap(db)
    _insert_audit(db, query="brain recall relevant answer", outcome="judged_good", actor="codex")
    sid = _insert_audit(db, query="한국어 recall wrong answer", actor="jenna")
    _insert_structural(db, sid, "structural_wrong")

    report = breakdown_mod.breakdown(hours=24, brain_db_path=db)

    assert report["status"] == "ok"
    assert report["total"] == 2
    assert report["wrong"] == 1
    assert report["by_label_source"]["llm_or_manual"]["total"] == 1
    assert report["by_label_source"]["structural_sidecar"]["wrong"] == 1
    assert report["by_language"]["ko"]["wrong"] == 1


def test_breakdown_prefers_llm_outcome_over_sidecar_for_same_audit_row(tmp_path: Path) -> None:
    db = tmp_path / "brain.db"
    _bootstrap(db)
    aid = _insert_audit(db, query="brain recall manually judged answer", outcome="judged_good")
    _insert_structural(db, aid, "structural_wrong")

    report = breakdown_mod.breakdown(hours=24, brain_db_path=db)

    assert report["total"] == 1
    assert report["wrong"] == 0
    assert report["by_label_source"] == {"llm_or_manual": {"total": 1, "wrong": 0, "wrong_rate": 0.0}}
