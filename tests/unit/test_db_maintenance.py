"""tests/unit/test_db_maintenance.py — retention + VACUUM safety net.

Asserts:
- run_autonomy_decisions_retention drops rows older than the configured
  window and leaves recent rows intact.
- run_metrics_history_retention behaves the same against metrics_snapshots.
- Both fail-closed (status == "db_missing") when the DB file is absent
  rather than crashing the scheduler.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from brain_core import db_maintenance


@pytest.fixture()
def autonomy_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "autonomy.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            """
            CREATE TABLE autonomy_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc TEXT NOT NULL,
                kind TEXT NOT NULL,
                level TEXT NOT NULL,
                allowed INTEGER NOT NULL,
                reason TEXT NOT NULL,
                breaker_state TEXT NOT NULL,
                context_json TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO autonomy_decisions (ts_utc, kind, level, allowed, reason, breaker_state) "
            "VALUES (?, 't', 'L1', 1, 'r', 'closed')",
            [
                ("2026-01-01T00:00:00+00:00",),  # ancient
                ("2026-04-01T00:00:00+00:00",),  # ~25d old
                ("2026-04-25T00:00:00+00:00",),  # 1d old
            ],
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(db_maintenance, "AUTONOMY_DB", db)
    return db


@pytest.fixture()
def metrics_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "metrics_history.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE metrics_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "timestamp TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO metrics_snapshots (timestamp, payload) VALUES (?, '{}')",
            [
                ("2026-01-01T00:00:00",),
                ("2026-04-01T00:00:00",),
                ("2026-04-25T00:00:00",),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(db_maintenance, "METRICS_HISTORY_DB", db)
    return db


def test_autonomy_decisions_retention_drops_older_rows(autonomy_db: Path) -> None:
    summary = db_maintenance.run_autonomy_decisions_retention(days=14)
    assert summary["status"] == "ok"
    assert summary["deleted"] == 2  # ancient + ~25d old
    assert summary["remaining"] == 1
    conn = sqlite3.connect(str(autonomy_db))
    try:
        rows = conn.execute("SELECT ts_utc FROM autonomy_decisions").fetchall()
    finally:
        conn.close()
    assert rows == [("2026-04-25T00:00:00+00:00",)]


def test_metrics_history_retention_drops_older_rows(metrics_db: Path) -> None:
    summary = db_maintenance.run_metrics_history_retention(days=14)
    assert summary["status"] == "ok"
    assert summary["deleted"] == 2
    assert summary["remaining"] == 1


def test_autonomy_decisions_retention_db_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(db_maintenance, "AUTONOMY_DB", tmp_path / "missing.db")
    summary = db_maintenance.run_autonomy_decisions_retention(days=14)
    assert summary["status"] == "db_missing"
    assert "deleted" not in summary  # no DELETE attempted


def test_metrics_history_retention_db_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(db_maintenance, "METRICS_HISTORY_DB", tmp_path / "missing.db")
    summary = db_maintenance.run_metrics_history_retention(days=14)
    assert summary["status"] == "db_missing"
