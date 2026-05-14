"""tests/unit/test_slo_replan.py — repeat-breach detector + task dedup."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "brain_core"))

import slo_replan as sr  # noqa: E402


def _write_log(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _trigger(slo: str, offset_hours: float, status: str = "rate_limited") -> dict:
    ts = (datetime.now(UTC) - timedelta(hours=offset_hours)).isoformat().replace("+00:00", "Z")
    return {
        "kind": "trigger",
        "slo": slo,
        "action": "log_rotation",
        "reason": "Brain logs exceeded size budget.",
        "current": 2500,
        "threshold": 2048,
        "status": status,
        "timestamp": ts,
    }


def test_find_repeat_breaches_counts_triggers_in_window(tmp_path: Path) -> None:
    log = tmp_path / "slo_remediation.jsonl"
    _write_log(
        log,
        [
            _trigger("logs_dir_total_mb", 1),
            _trigger("logs_dir_total_mb", 24),
            _trigger("logs_dir_total_mb", 48),
            _trigger("other_slo", 12),
            _trigger("ancient_slo", 8 * 24),  # outside 7d window
        ],
    )
    out = sr.find_repeat_breaches(log_path=log, window_days=7, min_triggers=3)
    names = [b["slo"] for b in out]
    assert names == ["logs_dir_total_mb"]
    assert out[0]["triggers"] == 3


def test_find_repeat_breaches_below_threshold(tmp_path: Path) -> None:
    log = tmp_path / "slo_remediation.jsonl"
    _write_log(log, [_trigger("slo_x", 1), _trigger("slo_x", 24)])
    out = sr.find_repeat_breaches(log_path=log, min_triggers=3)
    assert out == []


def test_materialize_review_tasks_dedupes(tmp_path: Path) -> None:
    log = tmp_path / "slo_remediation.jsonl"
    _write_log(log, [_trigger("slo_x", h) for h in (1, 12, 24, 48)])

    sig = sr._signature("slo_x")

    class _FakeTQ:
        def __init__(self) -> None:
            self.created: list[dict] = []

        def list_tasks(self, status=None):
            return [{"metadata": {"replan_signature": sig}}]

        def create_task(self, **kwargs):
            self.created.append(kwargs)
            return {"id": "t-1"}

    tq = _FakeTQ()
    out = sr.materialize_review_tasks(log_path=log, task_queue_obj=tq, min_triggers=3)
    assert out["created"] == []
    assert any(s["reason"] == "open_task_exists" for s in out["skipped"])
    assert tq.created == []


def test_materialize_review_tasks_creates_new(tmp_path: Path) -> None:
    log = tmp_path / "slo_remediation.jsonl"
    _write_log(log, [_trigger("slo_x", h) for h in (1, 12, 24, 48)])

    class _FakeTQ:
        def __init__(self) -> None:
            self.created: list[dict] = []

        def list_tasks(self, status=None):
            return []

        def create_task(self, **kwargs):
            self.created.append(kwargs)
            return {"id": "t-99"}

    tq = _FakeTQ()
    out = sr.materialize_review_tasks(log_path=log, task_queue_obj=tq, min_triggers=3)
    assert len(out["created"]) == 1
    assert tq.created[0]["assigned_agent"] == "brain_cli"
    assert tq.created[0]["metadata"]["replan_signature"] == sr._signature("slo_x")
    assert tq.created[0]["metadata"]["mutates_policy"] is False


def test_materialize_review_tasks_missing_log(tmp_path: Path) -> None:
    out = sr.materialize_review_tasks(log_path=tmp_path / "absent.jsonl")
    assert out == {"created": [], "skipped": [], "found": 0}
