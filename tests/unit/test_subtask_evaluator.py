"""tests/unit/test_subtask_evaluator.py — brain-quality subtask auto-completion.

Locks the "metric cleared → task complete" loop and the metadata
refresh path. The metric_snapshot function is stubbed so the tests
don't depend on live SLOs / outcomes traffic.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "brain_core"))

import subtask_evaluator  # noqa: E402
from task_queue import TaskQueue  # noqa: E402


def _seed_goal_and_subtask(
    tq: TaskQueue,
    metric_name: str,
    *,
    direction: str = "reduce_below",
    target: float = 50.0,
    current: float = 100.0,
) -> tuple[dict, dict]:
    goal = tq.create_goal("Brain self-quality", "test")
    task = tq.create_task(
        title=f"Drive {metric_name} ${direction} {target}",
        description="",
        assigned_agent="liz",
        priority=3,
        parent_goal_id=goal["id"],
        created_by="test",
        metadata={
            "brain_quality_metric": metric_name,
            "direction": direction,
            "current": current,
            "target": target,
            "unit": "%",
        },
    )
    return goal, task


def test_subtask_evaluator_completes_when_metric_clears(tmp_path, monkeypatch):
    db = tmp_path / "autonomy.db"
    tq = TaskQueue(db)
    _, task = _seed_goal_and_subtask(tq, "override_pct.coding", target=50.0)

    monkeypatch.setattr(
        subtask_evaluator,
        "_metric_snapshot",
        lambda _autonomy, _brain: {"override_pct.coding": 30.0},
    )

    result = subtask_evaluator.evaluate_brain_quality_subtasks(task_queue_obj=tq)
    assert len(result["completed"]) == 1
    assert result["completed"][0]["task_id"] == task["id"]

    updated = tq.get_task(task["id"])
    assert updated["status"] == "completed"


def test_subtask_evaluator_refreshes_when_metric_not_cleared(tmp_path, monkeypatch):
    db = tmp_path / "autonomy.db"
    tq = TaskQueue(db)
    _, task = _seed_goal_and_subtask(tq, "override_pct.infra", current=100.0, target=50.0)

    monkeypatch.setattr(
        subtask_evaluator,
        "_metric_snapshot",
        lambda _autonomy, _brain: {"override_pct.infra": 80.0},
    )

    result = subtask_evaluator.evaluate_brain_quality_subtasks(task_queue_obj=tq)
    assert result["completed"] == []
    assert len(result["refreshed"]) == 1
    assert result["refreshed"][0]["current"] == 80.0

    updated = tq.get_task(task["id"])
    meta = updated["metadata"]
    assert meta["current"] == 80.0
    assert meta.get("last_evaluated_by") == "subtask_evaluator"
    assert "last_evaluated_at" in meta
    assert updated["status"] == "pending"


def test_subtask_evaluator_raise_above_direction(tmp_path, monkeypatch):
    db = tmp_path / "autonomy.db"
    tq = TaskQueue(db)
    _, task = _seed_goal_and_subtask(
        tq, "recall_judge.judged_pct_7d", direction="raise_above", target=5.0, current=1.0
    )

    monkeypatch.setattr(
        subtask_evaluator,
        "_metric_snapshot",
        lambda _autonomy, _brain: {"recall_judge.judged_pct_7d": 6.5},
    )
    result = subtask_evaluator.evaluate_brain_quality_subtasks(task_queue_obj=tq)
    assert result["completed"][0]["task_id"] == task["id"]
    assert tq.get_task(task["id"])["status"] == "completed"


def test_subtask_evaluator_skips_when_metric_absent(tmp_path, monkeypatch):
    db = tmp_path / "autonomy.db"
    tq = TaskQueue(db)
    _, task = _seed_goal_and_subtask(tq, "override_pct.coding", current=100.0)

    monkeypatch.setattr(subtask_evaluator, "_metric_snapshot", lambda _a, _b: {})
    result = subtask_evaluator.evaluate_brain_quality_subtasks(task_queue_obj=tq)
    assert result["completed"] == []
    assert result["refreshed"] == []
    assert result["skipped"][0]["task_id"] == task["id"]
    assert result["skipped"][0]["reason"] == "metric_unavailable"


def test_judge_coverage_counts_structural_sidecar_without_action_outcome(tmp_path):
    db = tmp_path / "brain.db"
    now = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE action_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                route TEXT NOT NULL,
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
        cur = conn.execute(
            "INSERT INTO action_audit (route, outcome, created_at) VALUES ('/recall/v2', NULL, ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO recall_structural_judgments "
            "(action_audit_id, outcome, structural_score, reason_json, created_at, judged_at) "
            "VALUES (?, 'structural_neutral', 0.2, '{}', ?, ?)",
            (cur.lastrowid, now, now),
        )

    snap = subtask_evaluator._judge_coverage_snapshot(db)

    assert snap["recall_judge.judged_pct_7d"] == 100.0
