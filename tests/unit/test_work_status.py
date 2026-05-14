"""tests/unit/test_work_status.py — work_status shape + boot-block gating."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from brain_core import work_status


@pytest.fixture()
def history_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "scheduler_history.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            """
            CREATE TABLE job_history (
                id INTEGER PRIMARY KEY,
                job_name TEXT,
                started_at TEXT,
                pid INTEGER,
                error TEXT,
                manual INTEGER DEFAULT 0,
                finished_at TEXT DEFAULT NULL,
                duration_ms INTEGER DEFAULT NULL
            )
            """
        )
        conn.executemany(
            "INSERT INTO job_history (job_name, started_at, error) VALUES (?, ?, ?)",
            [
                ("flaky_job", "2026-05-14T20:00:00+00:00", "ConnectionError"),
                ("flaky_job", "2026-05-14T19:00:00+00:00", "ConnectionError"),
                ("ok_job", "2026-05-14T18:00:00+00:00", None),
                ("ok_job", "2026-05-14T17:00:00+00:00", ""),
                ("ancient_failure", "2026-04-01T00:00:00+00:00", "OldError"),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(work_status, "_SCHED_HISTORY_DB", db)
    return db


def test_compute_status_aggregates_recent_failures(history_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        work_status,
        "_scheduler_snapshot",
        lambda: [
            {"name": "running_now", "running_pid": 12345, "last_run": "2026-05-14T20:00:00+00:00"},
            {"name": "deferred_job", "resource_defer": "gpu_busy", "next_run": "2026-05-14T22:00:00-07:00"},
            {"name": "idle_job", "next_run": "2026-05-15T04:00:00-07:00"},
        ],
    )
    status = work_status.compute_status(window_hours=24 * 365)
    assert status["totals"]["running"] == 1
    # Aggregated across recent + ancient: 2 distinct job_names with errors
    failed_names = {f["job_name"] for f in status["recent_failures"]}
    assert "flaky_job" in failed_names
    assert "ok_job" not in failed_names
    assert status["totals"]["deferred"] == 1


def test_boot_context_block_returns_none_when_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(work_status, "_scheduler_snapshot", lambda: [])
    monkeypatch.setattr(work_status, "_recent_failures", lambda _h: [])
    assert work_status.boot_context_block() is None


def test_boot_context_block_emits_lines_on_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        work_status,
        "_scheduler_snapshot",
        lambda: [
            {"name": "active_job", "running_pid": 999, "last_run": "2026-05-14T20:00:00+00:00"},
        ],
    )
    monkeypatch.setattr(
        work_status,
        "_recent_failures",
        lambda _h: [
            {"job_name": "bad_job", "last_failed_at": "2026-05-14T19:00:00+00:00", "failures": 3},
        ],
    )
    block = work_status.boot_context_block()
    assert block is not None
    assert "RUNNING: active_job" in block
    assert "FAILED" in block and "bad_job" in block


def test_compute_status_missing_history_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(work_status, "_SCHED_HISTORY_DB", tmp_path / "nope.db")
    monkeypatch.setattr(work_status, "_scheduler_snapshot", lambda: [])
    status = work_status.compute_status()
    assert status["recent_failures"] == []
    assert status["totals"]["failed"] == 0
