from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "brain_core"))

import autonomous_work  # noqa: E402


def _iso(minutes_ago: int = 0) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def test_recent_autonomous_work_merges_dispatch_and_slo_records(tmp_path, monkeypatch):
    db = tmp_path / "autonomy.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                title TEXT,
                description TEXT,
                status TEXT,
                created_by TEXT,
                execution_log TEXT,
                metadata TEXT
            );
            CREATE TABLE task_dispatch_attempts (
                id TEXT PRIMARY KEY,
                task_id TEXT,
                trace_id TEXT,
                attempt_no INTEGER,
                agent TEXT,
                backend TEXT,
                model TEXT,
                status TEXT,
                error_class TEXT,
                error TEXT,
                result_preview TEXT,
                prompt_chars INTEGER,
                response_chars INTEGER,
                duration_ms INTEGER,
                started_at TEXT,
                completed_at TEXT,
                metadata TEXT
            );
            CREATE TABLE autonomy_decisions (
                id INTEGER PRIMARY KEY,
                ts_utc TEXT,
                kind TEXT,
                level TEXT,
                allowed INTEGER,
                reason TEXT,
                breaker_state TEXT,
                context_json TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "task_1",
                "Refresh background truth",
                "show what happened",
                "completed",
                "brain",
                json.dumps([{"from": "pending", "to": "approved", "by": "autopilot"}]),
                json.dumps({"trace_id": "trace_1"}),
            ),
        )
        conn.execute(
            """INSERT INTO task_dispatch_attempts VALUES
               (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "dispatch_1",
                "task_1",
                "trace_1",
                1,
                "sage",
                "codex",
                "gpt-5.5",
                "completed",
                "",
                "",
                "done",
                10,
                4,
                123,
                _iso(5),
                _iso(4),
                json.dumps({"source": "process_ready"}),
            ),
        )
        conn.execute(
            "INSERT INTO autonomy_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1,
                _iso(3),
                "task.dispatch",
                "L2",
                1,
                "notify_then_act",
                "closed",
                json.dumps({"task_id": "task_1"}),
            ),
        )
    log = tmp_path / "slo_remediation.jsonl"
    log.write_text(
        json.dumps(
            {
                "timestamp": _iso(2),
                "slo": "logs_dir_total_mb",
                "kind": "trigger",
                "action": "log_rotation",
                "status": "ok",
                "pid": 1234,
            }
        )
        + "\n"
    )
    monkeypatch.setattr(autonomous_work, "AUTONOMY_DB", db)
    monkeypatch.setattr(autonomous_work, "SLO_REMEDIATION_LOG", log)

    out = autonomous_work.recent_autonomous_work(limit=10, hours=1)

    assert out["status"] == "ok"
    assert out["visibility_gap_count"] == 0
    assert out["by_kind"]["task_dispatch"] == 1
    assert out["by_kind"]["slo_trigger"] == 1
    assert out["by_kind"]["authorization_no_prior_ack"] == 1
    assert out["by_consent"]["autopilot_no_prior_ack"] == 1


def test_visibility_gap_counts_malformed_concrete_records(tmp_path, monkeypatch):
    db = tmp_path / "autonomy.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """CREATE TABLE task_dispatch_attempts (
                id TEXT, task_id TEXT, trace_id TEXT, status TEXT,
                started_at TEXT, completed_at TEXT
            )"""
        )
        conn.execute(
            "INSERT INTO task_dispatch_attempts VALUES (?, ?, ?, ?, ?, ?)",
            ("dispatch_bad", "task_1", "", "completed", _iso(5), ""),
        )
    log = tmp_path / "slo_remediation.jsonl"
    log.write_text(
        json.dumps(
            {
                "timestamp": _iso(2),
                "slo": "logs_dir_total_mb",
                "kind": "trigger",
                "action": "log_rotation",
                "status": "ok",
            }
        )
        + "\n"
    )
    monkeypatch.setattr(autonomous_work, "AUTONOMY_DB", db)
    monkeypatch.setattr(autonomous_work, "SLO_REMEDIATION_LOG", log)

    assert autonomous_work.visibility_gap_count(hours=1) == 2


def test_recent_autonomous_work_includes_task_evaluation_decision(tmp_path, monkeypatch):
    db = tmp_path / "autonomy.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                title TEXT,
                description TEXT,
                status TEXT,
                assigned_agent TEXT,
                created_by TEXT,
                execution_log TEXT,
                metadata TEXT,
                updated_at TEXT
            )"""
        )
        conn.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "task_eval_1",
                "Inspect automatic task",
                "show what happened",
                "approved",
                "sage",
                "brain",
                json.dumps(
                    [
                        {
                            "event": "task_evaluation",
                            "decision": "handleable",
                            "action": "routed_for_agent_execution",
                        }
                    ]
                ),
                json.dumps(
                    {
                        "task_evaluation_action": "routed_for_agent_execution",
                        "task_evaluation_brain_action": "reassigned_to_sage_and_approved",
                        "task_evaluation_decision": "handleable",
                        "task_evaluation_reason": "LLM said Sage can inspect logs and report evidence",
                        "task_evaluation_source": "llm_handleable",
                        "task_evaluation_routed_at": _iso(3),
                        "task_evaluation_next_evidence": "/brain/tasks/task_eval_1/execution",
                    }
                ),
                _iso(3),
            ),
        )
    log = tmp_path / "slo_remediation.jsonl"
    log.write_text("")
    monkeypatch.setattr(autonomous_work, "AUTONOMY_DB", db)
    monkeypatch.setattr(autonomous_work, "SLO_REMEDIATION_LOG", log)

    out = autonomous_work.recent_autonomous_work(limit=10, hours=1)
    item = out["items"][0]

    assert item["kind"] == "task_evaluation"
    assert item["decision"] == "handleable"
    assert item["brain_action"] == "reassigned_to_sage_and_approved"
    assert item["llm_reason"] == "LLM said Sage can inspect logs and report evidence"
    assert item["next_evidence"] == "/brain/tasks/task_eval_1/execution"
    assert out["visibility_gap_count"] == 0


def test_task_evaluation_visibility_gap_requires_decision_details(tmp_path, monkeypatch):
    db = tmp_path / "autonomy.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE tasks (metadata TEXT)")
        conn.execute(
            "INSERT INTO tasks VALUES (?)",
            (json.dumps({"task_evaluation_routed_at": _iso(2), "task_evaluation_action": "handled"}),),
        )
    monkeypatch.setattr(autonomous_work, "AUTONOMY_DB", db)

    assert autonomous_work._task_evaluation_visibility_gaps(datetime.now(UTC) - timedelta(hours=1)) == 1
