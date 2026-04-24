from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core"))

import judgment_feedback
import judgment_layer


def _seed_judgment_row(
    conn: sqlite3.Connection,
    *,
    intent: str,
    needs_memory: int = 1,
    max_blocks: int = 4,
    max_tokens: int = 1400,
    min_score: float = 0.78,
    block_count: int = 4,
    semantic_count: int = 2,
    suppressed: dict | None = None,
    latency_ms: int = 10,
    outcome: str | None = None,
) -> None:
    action_id = None
    conn.execute(
        "CREATE TABLE IF NOT EXISTS action_audit (" "id INTEGER PRIMARY KEY AUTOINCREMENT, outcome TEXT)"
    )
    if outcome is not None:
        cur = conn.execute("INSERT INTO action_audit (outcome) VALUES (?)", (outcome,))
        action_id = cur.lastrowid
    conn.execute(
        "INSERT INTO active_recall_judgments "
        "(action_audit_id, actor, prompt_intent, needs_memory, allow_semantic, "
        " allow_proactive, max_blocks, max_tokens, min_semantic_score, "
        " block_count, semantic_count, suppressed_json, latency_ms, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
        (
            action_id,
            "codex",
            intent,
            needs_memory,
            1 if needs_memory else 0,
            1 if needs_memory else 0,
            max_blocks,
            max_tokens,
            min_score,
            block_count,
            semantic_count,
            json.dumps(suppressed or {}),
            latency_ms,
        ),
    )


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


def test_tuning_report_recommends_lower_threshold_when_bad_outcomes_below_score(tmp_path: Path) -> None:
    db = tmp_path / "brain.db"
    conn = sqlite3.connect(str(db))
    try:
        judgment_feedback._ensure_table(conn)
        for _ in range(10):
            _seed_judgment_row(
                conn,
                intent="implementation",
                suppressed={"below_intent_score": 1},
                outcome="restated",
            )
        for _ in range(10):
            _seed_judgment_row(
                conn,
                intent="implementation",
                suppressed={"below_intent_score": 1},
                outcome="judged_good",
            )
        conn.commit()
    finally:
        conn.close()

    report = judgment_feedback.tuning_report(hours=24, min_samples=20, db_path=db)
    rec = report["recommendations"][0]

    assert rec["action"] == "lower_semantic_threshold"
    assert rec["proposed_policy"]["min_semantic_score"] == 0.75


def test_tuning_report_handles_missing_action_audit_table(tmp_path: Path) -> None:
    db = tmp_path / "brain.db"
    conn = sqlite3.connect(str(db))
    try:
        judgment_feedback._ensure_table(conn)
        conn.execute(
            "INSERT INTO active_recall_judgments "
            "(action_audit_id, actor, prompt_intent, needs_memory, allow_semantic, "
            " allow_proactive, max_blocks, max_tokens, min_semantic_score, "
            " block_count, semantic_count, suppressed_json, latency_ms, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (None, "codex", "generic", 0, 0, 0, 0, 0, 1.0, 0, 0, "{}", 0),
        )
        conn.commit()
    finally:
        conn.close()

    report = judgment_feedback.tuning_report(hours=24, min_samples=5, db_path=db)

    assert report["recommendations"][0]["intent"] == "generic"
    assert report["recommendations"][0]["outcome_coverage"] == 0.0


def test_tuning_report_keeps_silent_gate_when_control_prompts_are_clean(tmp_path: Path) -> None:
    db = tmp_path / "brain.db"
    conn = sqlite3.connect(str(db))
    try:
        judgment_feedback._ensure_table(conn)
        for _ in range(20):
            _seed_judgment_row(
                conn,
                intent="execution_control",
                needs_memory=0,
                max_blocks=0,
                max_tokens=0,
                min_score=1.0,
                block_count=0,
                semantic_count=0,
                suppressed={},
                outcome="judged_good",
            )
        conn.commit()
    finally:
        conn.close()

    report = judgment_feedback.tuning_report(hours=24, min_samples=20, db_path=db)
    rec = report["recommendations"][0]

    assert rec["action"] == "keep_silent"
    assert rec["confidence"] == "high"


def test_tuning_report_recommends_budget_increase_when_budget_pressure_is_bad(tmp_path: Path) -> None:
    db = tmp_path / "brain.db"
    conn = sqlite3.connect(str(db))
    try:
        judgment_feedback._ensure_table(conn)
        for _ in range(20):
            _seed_judgment_row(
                conn,
                intent="policy_or_memory",
                max_blocks=5,
                max_tokens=1600,
                min_score=0.76,
                block_count=5,
                suppressed={"over_budget": 1},
                outcome="restated",
            )
        conn.commit()
    finally:
        conn.close()

    report = judgment_feedback.tuning_report(hours=24, min_samples=20, db_path=db)
    rec = report["recommendations"][0]

    assert rec["action"] == "increase_context_budget"
    assert rec["proposed_policy"]["max_blocks"] == 6
    assert rec["proposed_policy"]["max_tokens"] == 1800
