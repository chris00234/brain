"""Unit tests for brain_core.temporal_router (Phase D1)."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


@pytest.fixture
def router():
    if "temporal_router" in sys.modules:
        del sys.modules["temporal_router"]
    import temporal_router

    return temporal_router


NOW = datetime(2026, 4, 13, 22, 0, tzinfo=UTC)


def test_no_temporal_returns_empty(router):
    result = router.extract_temporal_intent("what is the chris korean name")
    assert result["has_temporal"] is False
    assert result["since"] is None


def test_iso_date_resolves_to_24h_window(router):
    result = router.extract_temporal_intent("2026-04-08에 어떤 shell session 있었어?", now=NOW)
    assert result["has_temporal"] is True
    assert result["kind"] == "point"
    assert result["since"].startswith("2026-04-08")
    assert result["until"].startswith("2026-04-09")


def test_iso_date_with_time_uses_30min_window(router):
    result = router.extract_temporal_intent("2026-04-08 05:25 UTC쯤 어떤 shell session", now=NOW)
    assert result["kind"] == "point"
    # since is 15 min before, until is 15 min after
    assert "05:10" in result["since"]
    assert "05:40" in result["until"]


def test_korean_month_day_uses_current_year(router):
    result = router.extract_temporal_intent("3월 14일 Chris screen time 패턴", now=NOW)
    assert result["has_temporal"] is True
    assert result["since"].startswith("2026-03-14")
    assert result["until"].startswith("2026-03-15")


def test_korean_full_date(router):
    result = router.extract_temporal_intent("2025년 12월 25일에 뭐했어?", now=NOW)
    assert result["since"].startswith("2025-12-25")


def test_english_month_day(router):
    result = router.extract_temporal_intent("what did I do April 8 last year?", now=NOW)
    assert result["has_temporal"] is True
    assert result["since"].startswith("2026-04-08")


def test_relative_days_ago(router):
    result = router.extract_temporal_intent("3 days ago", now=NOW)
    assert result["since"].startswith("2026-04-10")


def test_korean_days_ago(router):
    result = router.extract_temporal_intent("5일 전에 뭐했지", now=NOW)
    assert result["since"].startswith("2026-04-08")


def test_hours_ago_uses_narrow_window(router):
    result = router.extract_temporal_intent("3 hours ago what happened", now=NOW)
    assert result["has_temporal"] is True
    assert result["kind"] == "point"


def test_weeks_ago_returns_range(router):
    result = router.extract_temporal_intent("2 weeks ago", now=NOW)
    assert result["kind"] == "range"


def test_relative_recent(router):
    result = router.extract_temporal_intent("recent shell sessions", now=NOW)
    assert result["has_temporal"] is True
    assert result["kind"] == "recent"


def test_evolution_pattern(router):
    result = router.extract_temporal_intent("how did Chris's preference for FastAPI evolve?", now=NOW)
    assert result["has_temporal"] is True
    assert result["kind"] == "evolution"


def test_korean_evolution(router):
    result = router.extract_temporal_intent("Chris의 선호도가 시간이 지나면서 어떻게 바뀌었어?", now=NOW)
    assert result["kind"] == "evolution"


def test_lookup_raw_events_against_tmp_db(router, tmp_path):
    import sqlite3

    db = tmp_path / "brain.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE raw_events (
          id TEXT PRIMARY KEY, content_hash TEXT NOT NULL UNIQUE,
          timestamp TEXT NOT NULL, source_type TEXT NOT NULL,
          source_ref TEXT NOT NULL DEFAULT '', actor TEXT NOT NULL DEFAULT 'unknown',
          visibility TEXT NOT NULL DEFAULT 'private',
          scrub_status TEXT NOT NULL DEFAULT 'scrubbed',
          content TEXT NOT NULL,
          attachments_json TEXT NOT NULL DEFAULT '[]',
          entities_json TEXT NOT NULL DEFAULT '[]',
          json_path TEXT, created_at TEXT NOT NULL, processed_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO raw_events (id, content_hash, timestamp, source_type, content, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            "raw_1",
            "h1",
            "2026-04-08T05:25:00+00:00",
            "raw-shell_history",
            "ls -la /tmp",
            "2026-04-08T05:25:00+00:00",
        ),
    )
    conn.commit()
    conn.close()

    results = router.lookup_raw_events(
        since="2026-04-08T05:00:00+00:00",
        until="2026-04-08T06:00:00+00:00",
        db_path=db,
    )
    assert len(results) == 1
    assert results[0]["source"] == "raw-shell_history"
    assert results[0]["collection"] == "temporal_events"
