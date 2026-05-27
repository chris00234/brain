"""tests/unit/test_eval_regression_diff.py — P1-4 eval regression diff."""

from __future__ import annotations

import json
import sys
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


def _write_history(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def test_compute_diff_no_history(tmp_path, monkeypatch):
    import eval_regression_diff

    monkeypatch.setattr(eval_regression_diff, "LOGS_DIR", tmp_path)
    out = eval_regression_diff.compute_diff(track="extended")
    assert out["status"] == "no_history"


def test_compute_diff_single_run(tmp_path, monkeypatch):
    import eval_regression_diff

    monkeypatch.setattr(eval_regression_diff, "LOGS_DIR", tmp_path)
    _write_history(
        tmp_path / "eval-history-extended.jsonl",
        [{"timestamp": "2026-05-14T03:50:00", "failed_ids": ["a", "b"], "accuracy": 80.0}],
    )
    out = eval_regression_diff.compute_diff(track="extended")
    assert out["status"] == "single_run"
    assert out["current_failed"] == 2


def test_compute_diff_identifies_regressions_and_recoveries(tmp_path, monkeypatch):
    import eval_regression_diff

    monkeypatch.setattr(eval_regression_diff, "LOGS_DIR", tmp_path)
    # Yesterday failed {a, b}; today fails {b, c, d}.
    # newly_failing = {c, d}; newly_passing = {a}; persistent = {b}.
    _write_history(
        tmp_path / "eval-history-extended.jsonl",
        [
            {"timestamp": "2026-05-14T03:50:00", "failed_ids": ["a", "b"], "accuracy": 80.0},
            {
                "timestamp": "2026-05-15T03:50:00",
                "failed_ids": ["b", "c", "d"],
                "accuracy": 79.3,
            },
        ],
    )
    out = eval_regression_diff.compute_diff(track="extended")
    assert out["status"] == "ok"
    assert out["newly_failing"] == ["c", "d"]
    assert out["newly_passing"] == ["a"]
    assert out["persistent_failures"] == ["b"]
    assert out["delta_accuracy"] == -0.7
    assert "1 new fail" not in out["summary"]  # actually 2 new fail
    assert "2 new fail" in out["summary"]
    assert "1 recovered" in out["summary"]
    assert "1 persistent" in out["summary"]


def test_compute_diff_skips_rows_without_failed_ids(tmp_path, monkeypatch):
    """Legacy history rows pre-2026-05-15 lack `failed_ids`; the diff job
    must look past them to the most recent row that has the field."""
    import eval_regression_diff

    monkeypatch.setattr(eval_regression_diff, "LOGS_DIR", tmp_path)
    _write_history(
        tmp_path / "eval-history-extended.jsonl",
        [
            {"timestamp": "2026-04-01", "accuracy": 80.0},  # legacy, no failed_ids
            {"timestamp": "2026-05-14", "failed_ids": ["a"], "accuracy": 80.0},
            {"timestamp": "2026-05-15", "failed_ids": ["a", "b"], "accuracy": 79.3},
        ],
    )
    out = eval_regression_diff.compute_diff(track="extended")
    assert out["status"] == "ok"
    assert out["newly_failing"] == ["b"]
    assert out["persistent_failures"] == ["a"]


def test_write_diff_record_skips_when_current_row_stale(tmp_path, monkeypatch):
    """If the most recent eval-history row is older than the freshness
    threshold, the diff job must not publish a diff — it should report
    not_ready so triage doesn't get pointed at yesterday-vs-day-before."""
    from datetime import UTC, datetime, timedelta

    import eval_regression_diff

    monkeypatch.setattr(eval_regression_diff, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(eval_regression_diff, "FRESHNESS_THRESHOLD_HOURS", 6)

    stale = (datetime.now(UTC) - timedelta(hours=48)).isoformat(timespec="seconds")
    older = (datetime.now(UTC) - timedelta(hours=72)).isoformat(timespec="seconds")
    _write_history(
        tmp_path / "eval-history-extended.jsonl",
        [
            {"timestamp": older, "failed_ids": ["a"], "accuracy": 80.0},
            {"timestamp": stale, "failed_ids": ["b"], "accuracy": 80.0},
        ],
    )
    out = eval_regression_diff.write_diff_record(track="extended")
    assert out["status"] == "not_ready"
    assert out["write_status"] == "skipped"
    target = tmp_path / "eval-regression-diff-extended.jsonl"
    assert not target.exists(), "must not append a record when stale"


def test_write_diff_record_skips_duplicate_pair(tmp_path, monkeypatch):
    """Repeated cron firings against unchanged history must not multiply
    rows in the regression-diff log."""
    from datetime import UTC, datetime, timedelta

    import eval_regression_diff

    monkeypatch.setattr(eval_regression_diff, "LOGS_DIR", tmp_path)
    fresh = datetime.now(UTC).isoformat(timespec="seconds")
    prev = (datetime.now(UTC) - timedelta(hours=24)).isoformat(timespec="seconds")
    _write_history(
        tmp_path / "eval-history-extended.jsonl",
        [
            {"timestamp": prev, "failed_ids": ["a"], "accuracy": 80.0},
            {"timestamp": fresh, "failed_ids": ["b"], "accuracy": 80.0},
        ],
    )
    first = eval_regression_diff.write_diff_record(track="extended")
    second = eval_regression_diff.write_diff_record(track="extended")
    assert first["write_status"] == "ok"
    assert second["write_status"] == "skipped"
    assert second["reason"] == "duplicate_pair_already_recorded"
    target = tmp_path / "eval-regression-diff-extended.jsonl"
    assert len(target.read_text().splitlines()) == 1, "must not duplicate"


def test_write_diff_record_reports_error_status_on_oserror(tmp_path, monkeypatch):
    """If the JSONL append fails (full disk, perms), write_status must be
    'error' so the CLI can exit nonzero and the scheduler records a real
    failure instead of a silent success."""
    import eval_regression_diff

    monkeypatch.setattr(eval_regression_diff, "LOGS_DIR", tmp_path)
    _write_history(
        tmp_path / "eval-history-extended.jsonl",
        [
            {"timestamp": "2026-05-14", "failed_ids": ["a"], "accuracy": 80.0},
            {"timestamp": "2026-05-15", "failed_ids": ["b"], "accuracy": 80.0},
        ],
    )

    original_open = type(tmp_path).open

    def fail_open(self, *args, **kwargs):
        if "eval-regression-diff" in str(self):
            raise OSError("disk full")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(type(tmp_path), "open", fail_open)

    out = eval_regression_diff.write_diff_record(track="extended")
    assert out["write_status"] == "error"


def test_write_diff_record_appends_jsonl(tmp_path, monkeypatch):
    import eval_regression_diff

    monkeypatch.setattr(eval_regression_diff, "LOGS_DIR", tmp_path)
    _write_history(
        tmp_path / "eval-history-extended.jsonl",
        [
            {"timestamp": "2026-05-14", "failed_ids": ["a"], "accuracy": 80.0},
            {"timestamp": "2026-05-15", "failed_ids": ["b"], "accuracy": 80.0},
        ],
    )
    out = eval_regression_diff.write_diff_record(track="extended")
    assert out["write_status"] == "ok"
    target = tmp_path / "eval-regression-diff-extended.jsonl"
    assert target.exists()
    row = json.loads(target.read_text().strip())
    assert row["newly_failing"] == ["b"]
    assert row["newly_passing"] == ["a"]
