"""tests/unit/test_outcome_feedback.py — override pattern → review task pipeline.

Locks the chris_override -> learning loop: it must group by domain +
override_reason, rank by severity, and create bounded, dedupe-safe review
tasks without ever mutating policy.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "brain_core"))

from outcome_feedback import (  # noqa: E402
    create_override_review_tasks,
    override_patterns_report,
)
from task_queue import TaskQueue  # noqa: E402


def _iso(hours_ago: float = 0.0) -> str:
    return (
        (datetime.now(UTC) - timedelta(hours=hours_ago))
        .isoformat(timespec="seconds")
        .replace("+00:00", "+00:00")
    )


def _seed_outcomes(db_path: Path, rows: list[dict]) -> None:
    """Insert directly into outcomes — the table schema is owned by
    TaskQueue._migrate, so the queue is initialised once to set up the
    schema, then we INSERT raw test rows."""
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
                    r.get("brain_recommendation", ""),
                    r.get("actual_action", ""),
                    int(r.get("chris_override", 0)),
                    r.get("override_reason", ""),
                    float(r.get("confidence_was", 0.0)),
                    r.get("created_at"),
                )
                for r in rows
            ],
        )


def test_report_groups_overrides_by_domain_and_reason(tmp_path: Path) -> None:
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
            },
            {
                "id": "o2",
                "task_id": "t2",
                "domain": "infra",
                "chris_override": 1,
                "override_reason": "wrong port",
                "created_at": _iso(2),
            },
            {
                "id": "o3",
                "task_id": "t3",
                "domain": "infra",
                "chris_override": 0,
                "override_reason": "",
                "created_at": _iso(2),
            },
            {
                "id": "o4",
                "task_id": "t4",
                "domain": "coding",
                "chris_override": 1,
                "override_reason": "wrong API",
                "created_at": _iso(3),
            },
        ],
    )
    report = override_patterns_report(hours=24, min_overrides=2, db_path=db)
    assert report["sampled_outcomes"] == 4
    candidates = report["learning_candidates"]
    sigs = {c["domain"]: c for c in candidates}
    # 'infra/wrong port' has 2 overrides — promoted.
    assert "infra" in sigs
    assert sigs["infra"]["overrides"] == 2
    assert sigs["infra"]["override_rate"] == round(2 / 3, 4)
    # 'coding/wrong API' only had 1 override; below min_overrides → dropped.
    assert "coding" not in sigs
    assert report["contract"]["mutates_policy"] is False
    assert report["contract"]["uses_llm"] is False


def test_report_skips_non_override_outcomes(tmp_path: Path) -> None:
    db = tmp_path / "autonomy.db"
    _seed_outcomes(
        db,
        [
            {
                "id": "o1",
                "task_id": "t1",
                "domain": "infra",
                "chris_override": 0,
                "override_reason": "",
                "created_at": _iso(1),
            }
        ],
    )
    report = override_patterns_report(hours=24, min_overrides=1, db_path=db)
    assert report["learning_candidates"] == []
    assert report["sampled_outcomes"] == 1


def test_report_handles_missing_db(tmp_path: Path) -> None:
    report = override_patterns_report(hours=24, db_path=tmp_path / "missing.db")
    assert report["sampled_outcomes"] == 0
    assert report["learning_candidates"] == []
    assert report["contract"]["skipped_reason"] == "autonomy_db_missing"


def test_create_review_tasks_is_idempotent_via_signature(tmp_path: Path) -> None:
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
                "created_at": _iso(1 + i * 0.1),
            }
            for i in range(3)
        ],
    )
    tq = TaskQueue(db)

    first = create_override_review_tasks(
        hours=24, min_overrides=2, max_tasks=5, db_path=db, task_queue_obj=tq
    )
    second = create_override_review_tasks(
        hours=24, min_overrides=2, max_tasks=5, db_path=db, task_queue_obj=tq
    )
    assert len(first["created"]) == 1
    assert len(second["created"]) == 0
    assert second["skipped"][0]["reason"] == "open_task_exists"

    # Verify task metadata carries the contract markers + signature.
    pending = tq.list_tasks(status="pending")
    assert len(pending) == 1
    meta = pending[0]["metadata"]
    assert meta["source"] == "outcome_feedback"
    assert meta["override_signature"].startswith("ov_")
    assert meta["mutates_policy"] is False
    # 2026-05-13: review tasks now dispatch through cli_llm (codex →
    # claude) rather than an OpenClaw persona — `uses_llm` is true and
    # `llm_dispatch` names the path.
    assert meta["uses_llm"] is True
    assert meta["llm_dispatch"] == "cli_llm"


def test_severity_prefers_recent_high_volume_patterns(tmp_path: Path) -> None:
    db = tmp_path / "autonomy.db"
    rows: list[dict] = []
    # Old domain: 5 overrides, all >7d ago → recency weight ≈ 0
    for i in range(5):
        rows.append(
            {
                "id": f"old{i}",
                "task_id": f"old_t{i}",
                "domain": "infra",
                "chris_override": 1,
                "override_reason": "old reason",
                "created_at": _iso(24 * 14),  # 14d ago
            }
        )
    # Fresh domain: 4 overrides in last 24h → recency weight 1.0
    for i in range(4):
        rows.append(
            {
                "id": f"new{i}",
                "task_id": f"new_t{i}",
                "domain": "infra",
                "chris_override": 1,
                "override_reason": "fresh reason",
                "created_at": _iso(1 + i * 0.5),
            }
        )
    _seed_outcomes(db, rows)
    report = override_patterns_report(hours=24 * 30, min_overrides=2, db_path=db)
    by_reason = {c["override_reason"]: c for c in report["learning_candidates"]}
    assert "fresh reason" in by_reason
    assert "old reason" in by_reason
    # Severity is the rank key — recent overrides must dominate old ones.
    assert by_reason["fresh reason"]["severity"] > by_reason["old reason"]["severity"]
    assert report["learning_candidates"][0]["override_reason"] == "fresh reason"
