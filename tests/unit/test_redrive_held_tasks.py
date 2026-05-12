from __future__ import annotations

import json
import sys
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))
sys.path.insert(0, str(BRAIN_ROOT / "cli"))

from redrive_held_tasks import redrive_held_tasks  # noqa: E402
from task_queue import TaskQueue  # noqa: E402


def _held_metadata(source: str = "classifier_human_required") -> dict:
    return {
        "task_evaluation_decision": "policy_held"
        if source == "classifier_human_required"
        else "human_needed",
        "task_evaluation_source": source,
        "task_evaluation_action": "held_for_safe_followup",
        "task_evaluation_reason": "explicit_human_request",
        "task_evaluation_routed_at": "2026-05-07T20:00:00+00:00",
        "last_escalated_at": "2026-05-07T20:00:00+00:00",
    }


def test_redrive_reclassifies_classifier_held_task(tmp_path):
    db = tmp_path / "autonomy.db"
    tq = TaskQueue(db)
    task = tq.create_task(
        title=(
            "Handoff from brain_speak_urgent: Urgent Brain observation with no active CLI session. "
            "Handle it yourself if possible; notify Chris only for a true human blocker."
        ),
        description="[brain_speak_urgent] no active CLI sessions; urgent observations: stale task backlog",
        assigned_agent="sage",
        confidence=0.0,
        metadata=_held_metadata(),
    )

    summary = redrive_held_tasks(db)
    updated = tq.get_task(task["id"])

    assert summary["scanned"] == 1
    assert summary["redriven"] == 1
    assert summary["still_held"] == 0
    assert summary["redriven_task_ids"] == [task["id"]]
    assert updated["status"] == "pending"
    assert "last_escalated_at" not in updated["metadata"]
    assert updated["metadata"]["task_evaluation_decision"] == "pending_redrive"
    assert updated["metadata"]["task_evaluation_source"] == "classifier_redrive"
    assert updated["metadata"]["task_evaluation_redrive_previous"]["source"] == "classifier_human_required"
    assert any(row.get("event") == "task_evaluation_redrive" for row in updated["execution_log"])


def test_redrive_leaves_still_human_classifier_hold_untouched(tmp_path):
    db = tmp_path / "autonomy.db"
    tq = TaskQueue(db)
    task = tq.create_task(
        title="Delete production data",
        description="Irreversible wipe requested",
        confidence=0.0,
        metadata={**_held_metadata(), "requires_human": True},
    )

    summary = redrive_held_tasks(db)
    updated = tq.get_task(task["id"])

    assert summary["scanned"] == 1
    assert summary["redriven"] == 0
    assert summary["still_held"] == 1
    assert updated["metadata"] == task["metadata"]
    assert updated["execution_log"] == task["execution_log"]


def test_redrive_never_overrides_llm_human_needed(tmp_path):
    db = tmp_path / "autonomy.db"
    tq = TaskQueue(db)
    task = tq.create_task(
        title=(
            "Handoff from brain_speak_urgent: Urgent Brain observation with no active CLI session. "
            "Handle it yourself if possible; notify Chris only for a true human blocker."
        ),
        description="Stale task backlog",
        confidence=0.0,
        metadata=_held_metadata("llm_human_needed"),
    )

    summary = redrive_held_tasks(db)
    updated = tq.get_task(task["id"])

    assert summary["scanned"] == 0
    assert summary["redriven"] == 0
    assert updated["metadata"] == task["metadata"]


def test_redrive_skips_non_pending_tasks(tmp_path):
    db = tmp_path / "autonomy.db"
    tq = TaskQueue(db)
    task = tq.create_task(
        title="Handoff from brain_speak_urgent: Handle it yourself if possible; notify Chris only for a true human blocker.",
        description="Stale task backlog",
        confidence=0.0,
        metadata=_held_metadata(),
    )
    tq.approve_task(task["id"], by="test")

    summary = redrive_held_tasks(db)
    updated = tq.get_task(task["id"])

    assert summary["scanned"] == 0
    assert updated["status"] == "approved"
    assert updated["metadata"]["task_evaluation_source"] == "classifier_human_required"


def test_redrive_is_idempotent(tmp_path):
    db = tmp_path / "autonomy.db"
    tq = TaskQueue(db)
    task = tq.create_task(
        title=(
            "Handoff from brain_speak_urgent: Urgent Brain observation with no active CLI session. "
            "Handle it yourself if possible; notify Chris only for a true human blocker."
        ),
        description="Stale task backlog",
        confidence=0.0,
        metadata=_held_metadata(),
    )

    first = redrive_held_tasks(db)
    second = redrive_held_tasks(db)
    updated = tq.get_task(task["id"])

    assert first["redriven"] == 1
    assert second["scanned"] == 0
    assert second["redriven"] == 0
    assert [row.get("event") for row in updated["execution_log"]].count("task_evaluation_redrive") == 1


def test_redrive_does_not_import_telegram(tmp_path, monkeypatch):
    db = tmp_path / "autonomy.db"
    tq = TaskQueue(db)
    tq.create_task(
        title="Handoff from brain_speak_urgent: Handle it yourself if possible; notify Chris only for a true human blocker.",
        description="Stale task backlog",
        confidence=0.0,
        metadata=_held_metadata(),
    )
    monkeypatch.delitem(sys.modules, "telegram_alert", raising=False)

    redrive_held_tasks(db)

    assert "telegram_alert" not in sys.modules


def test_redrive_dry_run_does_not_write(tmp_path):
    db = tmp_path / "autonomy.db"
    tq = TaskQueue(db)
    task = tq.create_task(
        title="Handoff from brain_speak_urgent: Handle it yourself if possible; notify Chris only for a true human blocker.",
        description="Stale task backlog",
        confidence=0.0,
        metadata=_held_metadata(),
    )

    summary = redrive_held_tasks(db, dry_run=True)
    updated = tq.get_task(task["id"])

    assert summary["dry_run"] is True
    assert summary["redriven"] == 1
    assert updated["metadata"] == task["metadata"]


def test_redrive_cli_json_summary(tmp_path, capsys):
    db = tmp_path / "autonomy.db"
    TaskQueue(db).create_task(
        title="Handoff from brain_speak_urgent: Handle it yourself if possible; notify Chris only for a true human blocker.",
        description="Stale task backlog",
        confidence=0.0,
        metadata=_held_metadata(),
    )

    import redrive_held_tasks as cli

    assert cli.main(["--db", str(db), "--dry-run"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["scanned"] == 1
    assert out["redriven"] == 1
