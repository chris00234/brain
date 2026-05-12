from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))

from task_lifecycle_hygiene import (  # noqa: E402
    apply_safe_repairs,
    audit_task_lifecycle,
    task_lifecycle_hygiene_issue_count,
)
from task_queue import TaskQueue  # noqa: E402


def _mark_completed_with_error(db: Path, task_id: str) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE tasks SET status='completed', completed_at=?, result='done', error='backend_cooldown 10s' WHERE id=?",
            (datetime.now(UTC).isoformat(timespec="seconds"), task_id),
        )


def test_lifecycle_audit_counts_critical_issues(tmp_path):
    db = tmp_path / "autonomy.db"
    tq = TaskQueue(db)
    completed = tq.create_task("Done but stale error", confidence=0.9)
    _mark_completed_with_error(db, completed["id"])
    tq.create_task(
        title="Held by old classifier",
        confidence=0.0,
        metadata={
            "task_evaluation_decision": "policy_held",
            "task_evaluation_source": "classifier_human_required",
        },
    )
    running = tq.create_task("Stale running", confidence=0.9)
    tq.approve_task(running["id"], by="test")
    tq.start_task(running["id"], by="test")
    stale_started = (datetime.now(UTC) - timedelta(hours=2)).isoformat(timespec="seconds")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE tasks SET started_at=? WHERE id=?", (stale_started, running["id"]))
        conn.execute(
            """INSERT INTO task_dispatch_attempts
               (id, task_id, trace_id, attempt_no, status, started_at)
               VALUES ('dispatch_stale', ?, ?, 1, 'started', ?)""",
            (running["id"], running["id"], stale_started),
        )

    report = audit_task_lifecycle(db, now=datetime.now(UTC))

    assert report["status"] == "blocked"
    assert report["readiness_blocking"] is True
    assert report["completed_with_error_count"] == 1
    assert report["classifier_held_redrivable_count"] == 1
    assert report["stale_running_task_count"] == 1
    assert report["stale_started_dispatch_count"] == 1
    assert report["critical_issue_count"] == 4
    assert task_lifecycle_hygiene_issue_count(db) == 4


def test_lifecycle_audit_reports_backlog_warnings_without_blocking(tmp_path):
    db = tmp_path / "autonomy.db"
    tq = TaskQueue(db)
    first = tq.create_task("Duplicate task", "same", confidence=0.9)
    second = tq.create_task("Duplicate task", "same", confidence=0.9)
    tq.approve_task(first["id"], by="test")
    tq.approve_task(second["id"], by="test")

    report = audit_task_lifecycle(db, now=datetime.now(UTC))

    assert report["status"] == "warning"
    assert report["readiness_blocking"] is False
    assert report["critical_issue_count"] == 0
    assert report["ready_backlog_count"] == 2
    assert report["duplicate_exact_shadow_count"] == 1
    assert report["samples"]["duplicate_exact_groups"][0]["canonical_task_id"] == first["id"]
    assert report["samples"]["duplicate_exact_groups"][0]["duplicate_task_ids"] == [second["id"]]


def test_lifecycle_audit_does_not_block_on_currently_valid_human_hold(tmp_path):
    db = tmp_path / "autonomy.db"
    tq = TaskQueue(db)
    task = tq.create_task(
        title="Confirm private deployment knowledge",
        description="missing context before deciding whether Chris approval is required",
        confidence=0.0,
        metadata={
            "task_evaluation_decision": "policy_held",
            "task_evaluation_source": "classifier_human_required",
            "task_evaluation_reason": "missing_private_knowledge",
        },
    )

    report = audit_task_lifecycle(db, now=datetime.now(UTC))

    assert report["status"] == "ok"
    assert report["readiness_blocking"] is False
    assert report["critical_issue_count"] == 0
    assert report["pending_policy_held_count"] == 1
    assert report["classifier_held_redrivable_count"] == 0
    assert report["classifier_held_still_human_count"] == 1
    assert report["samples"]["classifier_held_still_human_task_ids"] == [task["id"]]


def test_apply_safe_repairs_clears_only_completed_stale_errors(tmp_path):
    db = tmp_path / "autonomy.db"
    tq = TaskQueue(db)
    completed = tq.create_task("Completed stale error", confidence=0.9)
    _mark_completed_with_error(db, completed["id"])
    pending = tq.create_task("Pending has real error", confidence=0.0)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE tasks SET error='real pending error' WHERE id=?", (pending["id"],))

    summary = apply_safe_repairs(db)
    fixed = tq.get_task(completed["id"])
    untouched = tq.get_task(pending["id"])

    assert summary["completed_errors_cleared"] == 1
    assert fixed["error"] == ""
    assert any(row.get("event") == "completed_error_cleared" for row in fixed["execution_log"])
    assert untouched["error"] == "real pending error"


def test_lifecycle_cli_outputs_report_and_repair(tmp_path, capsys):
    db = tmp_path / "autonomy.db"
    tq = TaskQueue(db)
    task = tq.create_task("Completed stale error", confidence=0.9)
    _mark_completed_with_error(db, task["id"])

    spec = importlib.util.spec_from_file_location(
        "task_lifecycle_hygiene_cli", BRAIN_ROOT / "cli" / "task_lifecycle_hygiene.py"
    )
    assert spec and spec.loader
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    assert cli.main(["--db", str(db), "--apply-safe"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["safe_repair"]["completed_errors_cleared"] == 1
    assert out["completed_with_error_count"] == 0
