from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core"))

import judgment_feedback
import judgment_layer


def test_record_and_report_judgment_feedback(tmp_path: Path) -> None:
    db = tmp_path / "brain.db"
    judgment = judgment_layer.classify_prompt("좋다 진행하자")
    arbitration = judgment_layer.ArbitrationResult(blocks=[], suppressed={"memory_not_needed": 2})

    judgment_feedback.record(
        action_audit_id=123,
        session_id="sess",
        actor="codex",
        judgment=judgment,
        arbitration=arbitration,
        block_count=0,
        semantic_count=0,
        latency_ms=3,
        db_path=db,
    )

    with sqlite3.connect(str(db)) as conn:
        row = conn.execute("SELECT * FROM active_recall_judgments").fetchone()
    assert row is not None

    report = judgment_feedback.report(hours=24, db_path=db)

    assert report["by_intent"][0]["intent"] == "execution_control"
    assert report["by_intent"][0]["suppressed_prompts"] == 1
    assert report["suppressed"] == {"memory_not_needed": 2}


def test_record_ignores_missing_judgment(tmp_path: Path) -> None:
    db = tmp_path / "brain.db"

    judgment_feedback.record(
        action_audit_id=None,
        session_id=None,
        actor=None,
        judgment=None,
        arbitration=None,
        block_count=0,
        semantic_count=0,
        latency_ms=0,
        db_path=db,
    )

    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='active_recall_judgments'"
        ).fetchall()
    finally:
        conn.close()
    assert rows == []


def test_report_sums_suppressed_json(tmp_path: Path) -> None:
    db = tmp_path / "brain.db"
    conn = sqlite3.connect(str(db))
    try:
        judgment_feedback._ensure_table(conn)
        for suppressed in ({"stale_or_superseded": 1}, {"stale_or_superseded": 2, "near_duplicate": 1}):
            conn.execute(
                "INSERT INTO active_recall_judgments "
                "(action_audit_id, actor, prompt_intent, needs_memory, allow_semantic, "
                " allow_proactive, max_blocks, max_tokens, min_semantic_score, "
                " block_count, semantic_count, suppressed_json, latency_ms, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
                (
                    None,
                    "codex",
                    "policy_or_memory",
                    1,
                    1,
                    1,
                    5,
                    1600,
                    0.76,
                    1,
                    1,
                    json.dumps(suppressed),
                    10,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    report = judgment_feedback.report(hours=24, db_path=db)

    assert report["suppressed"] == {"near_duplicate": 1, "stale_or_superseded": 3}
