"""tests/unit/test_goal_subtask_scaffold.py — deterministic brain-quality
subtask materialization.

Covers the read-only proposer (no SLO/override traffic ⇒ no proposals),
the idempotent materializer (same metric ⇒ skipped on re-run), and goal
selection by title match (Korean and English tokens).
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "brain_core"))

import goal_subtask_scaffold  # noqa: E402
from task_queue import TaskQueue  # noqa: E402


def _iso(hours_ago: float = 0.0) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


def _seed_outcomes(db_path: Path, rows: list[dict]) -> None:
    TaskQueue(db_path)  # run migrations
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO outcomes
              (id, task_id, domain, brain_recommendation, actual_action,
               chris_override, override_reason, confidence_was,
               procedure_ids, lesson_ids, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, '[]', '[]', ?)
            """,
            [
                (
                    r["id"],
                    r["task_id"],
                    r.get("domain", "general"),
                    "",
                    r.get("actual_action", ""),
                    int(r.get("chris_override", 0)),
                    r.get("override_reason", ""),
                    0.0,
                    r.get("created_at"),
                )
                for r in rows
            ],
        )


@pytest.fixture
def stub_slo_and_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the SLO and judge-volume proposers to empty so the override-pattern
    proposal is isolated for the tests below."""

    def _no_breaches(send_alerts: bool = False) -> dict:
        return {"items": []}

    # Stub the imports inside the proposer functions.
    monkeypatch.setattr(
        goal_subtask_scaffold,
        "_slo_breach_proposals",
        lambda: [],
    )
    monkeypatch.setattr(
        goal_subtask_scaffold,
        "_judge_volume_proposals",
        lambda _brain_db: [],
    )
    monkeypatch.setattr(
        goal_subtask_scaffold,
        "_uncertainty_proposals",
        lambda _brain_db: [],
    )


def test_proposer_promotes_high_override_domains(tmp_path: Path, stub_slo_and_judge: None) -> None:
    db = tmp_path / "autonomy.db"
    _seed_outcomes(
        db,
        [
            {
                "id": f"o{i}",
                "task_id": f"t{i}",
                "domain": "infra",
                "chris_override": 1,
                "override_reason": "wrong port",
                "created_at": _iso(1 + i * 0.05),
            }
            for i in range(5)
        ],
    )
    proposals = goal_subtask_scaffold.propose_brain_quality_subtasks(autonomy_db_path=db)
    metric_names = {p["metric_name"] for p in proposals}
    assert "override_pct.infra" in metric_names
    infra = next(p for p in proposals if p["metric_name"] == "override_pct.infra")
    assert infra["direction"] == "reduce_below"
    assert infra["current"] == 100.0
    assert infra["target"] == 50.0
    assert infra["unit"] == "%"


def test_ensure_is_idempotent(tmp_path: Path, stub_slo_and_judge: None) -> None:
    db = tmp_path / "autonomy.db"
    _seed_outcomes(
        db,
        [
            {
                "id": f"o{i}",
                "task_id": f"t{i}",
                "domain": "infra",
                "chris_override": 1,
                "override_reason": "wrong port",
                "created_at": _iso(1 + i * 0.05),
            }
            for i in range(3)
        ],
    )
    tq = TaskQueue(db)
    goal = tq.create_goal("Brain self-quality", "Drive override rate down.")

    first = goal_subtask_scaffold.ensure_brain_quality_subtasks(
        goal_id=goal["id"],
        task_queue_obj=tq,
        autonomy_db_path=db,
    )
    second = goal_subtask_scaffold.ensure_brain_quality_subtasks(
        goal_id=goal["id"],
        task_queue_obj=tq,
        autonomy_db_path=db,
    )
    assert len(first["created"]) == 1
    assert len(second["created"]) == 0
    assert second["skipped"][0]["reason"] == "open_subtask_exists"

    subtasks = tq.list_tasks(parent_goal_id=goal["id"])
    assert len(subtasks) == 1
    meta = subtasks[0]["metadata"]
    assert meta["brain_quality_metric"] == "override_pct.infra"
    assert meta["target"] == 50.0
    assert meta["mutates_policy"] is False
    # 2026-05-13: subtasks dispatched through cli_llm by review_task_dispatcher.
    assert meta["uses_llm"] is True
    assert meta["llm_dispatch"] == "cli_llm"


def test_ensure_picks_goal_by_title_match(tmp_path: Path, stub_slo_and_judge: None) -> None:
    db = tmp_path / "autonomy.db"
    _seed_outcomes(
        db,
        [
            {
                "id": f"o{i}",
                "task_id": f"t{i}",
                "domain": "coding",
                "chris_override": 1,
                "override_reason": "wrong API",
                "created_at": _iso(1 + i * 0.05),
            }
            for i in range(4)
        ],
    )
    tq = TaskQueue(db)
    tq.create_goal("Side project: refactor portfolio", "Unrelated goal.")
    target = tq.create_goal(
        "Brain을 세계 최고 수준의 인간-뇌 대체 시스템으로 완성",
        "Brain self-quality drive.",
    )

    result = goal_subtask_scaffold.ensure_brain_quality_subtasks(
        task_queue_obj=tq,
        autonomy_db_path=db,
    )
    assert result["goal_id"] == target["id"]
    subtasks = tq.list_tasks(parent_goal_id=target["id"])
    assert len(subtasks) == 1


def test_ensure_no_matching_goal(tmp_path: Path, stub_slo_and_judge: None) -> None:
    db = tmp_path / "autonomy.db"
    _seed_outcomes(
        db,
        [
            {
                "id": "o1",
                "task_id": "t1",
                "domain": "infra",
                "chris_override": 1,
                "override_reason": "wrong port",
                "created_at": _iso(1),
            }
        ],
    )
    tq = TaskQueue(db)
    tq.create_goal("Side project", "No match.")
    result = goal_subtask_scaffold.ensure_brain_quality_subtasks(
        task_queue_obj=tq,
        autonomy_db_path=db,
    )
    assert result["error"] == "no_matching_goal"
    assert result["created"] == []
