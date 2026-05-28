"""Unit tests for SLOs added on 2026-04-17."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core"))


def _slos():
    import importlib

    import slos

    importlib.reload(slos)
    return slos


def test_dispatch_failure_rate_with_no_logs(tmp_path, monkeypatch):
    slos = _slos()
    monkeypatch.setattr(slos, "BRAIN_LOGS_DIR", tmp_path)
    assert slos._measure_dispatch_failure_rate_1h() == 0.0


def test_dispatch_failure_rate_counts_recent_failures(tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta

    slos = _slos()
    monkeypatch.setattr(slos, "BRAIN_LOGS_DIR", tmp_path)

    # 2 recent failures (within last hour, zone-naive strings)
    now_naive = datetime.now(UTC).replace(tzinfo=None)
    recent = (now_naive - timedelta(minutes=30)).isoformat()
    old = (now_naive - timedelta(hours=4)).isoformat()

    failures_path = tmp_path / "dispatch-failures.jsonl"
    failures_path.write_text(
        json.dumps({"timestamp": recent, "agent": "jenna", "error": "empty"})
        + "\n"
        + json.dumps({"timestamp": recent, "agent": "jenna", "error": "empty"})
        + "\n"
        + json.dumps({"timestamp": old, "agent": "jenna", "error": "empty"})
        + "\n"
    )

    # Insert 10 dispatches in llm_usage.db for last hour
    import sqlite3

    db_path = tmp_path / "llm_usage.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE llm_usage ("
        "timestamp TEXT, agent TEXT, duration_ms INTEGER, ok INTEGER, "
        "prompt_tokens INTEGER, response_tokens INTEGER, skipped_cb INTEGER, "
        "provider TEXT, model TEXT, cache_read_tokens INTEGER, cache_write_tokens INTEGER, cost_usd REAL)"
    )
    for _ in range(10):
        conn.execute(
            "INSERT INTO llm_usage (timestamp, agent, ok) VALUES (?, ?, 1)",
            (recent, "jenna"),
        )
    conn.commit()
    conn.close()

    rate = slos._measure_dispatch_failure_rate_1h()
    # 2 failures / 10 dispatches = 20% (exact because only 2 recent + 10 total)
    assert 10.0 <= rate <= 30.0, f"expected ~20%, got {rate}"


def test_agent_session_max_mb(tmp_path, monkeypatch):
    slos = _slos()
    # Point Path.home() to tmp_path indirectly by monkeypatching
    agents = tmp_path / ".hermes" / "profiles" / "jenna" / "sessions"
    agents.mkdir(parents=True)
    # Small session
    (agents / "small.jsonl").write_bytes(b"X" * 1024)
    # Big session: 50MB
    (agents / "big.jsonl").write_bytes(b"Y" * (50 * 1024 * 1024))
    # Checkpoint (should be ignored)
    (agents / "abc.checkpoint.x.jsonl").write_bytes(b"Z" * (60 * 1024 * 1024))

    monkeypatch.setenv("HOME", str(tmp_path))
    # Re-run; Path.home() reads HOME env
    assert slos._measure_agent_session_max_mb() == 50.0


def test_logs_dir_total_mb(tmp_path, monkeypatch):
    slos = _slos()
    monkeypatch.setattr(slos, "BRAIN_LOGS_DIR", tmp_path)
    # Create a few files summing to ~3MB
    (tmp_path / "a.log").write_bytes(b"A" * 1024 * 1024)
    (tmp_path / "b.log").write_bytes(b"B" * 1024 * 1024)
    sub = tmp_path / "jobs"
    sub.mkdir()
    (sub / "c.log").write_bytes(b"C" * 1024 * 1024)
    total = slos._measure_logs_dir_total_mb()
    assert 2.9 <= total <= 3.1


def test_is_breach_directions():
    slos = _slos()
    # higher-is-better: breach when below target
    higher_is_better = slos.SLOS["recall_v2_content_hit_pct"]
    assert slos._is_breach(higher_is_better, 94.0) is True
    assert slos._is_breach(higher_is_better, 96.0) is False

    # lower-is-better: breach when above target
    lower_is_better = slos.SLOS["breaker_open_count"]
    assert slos._is_breach(lower_is_better, 1.0) is True
    assert slos._is_breach(lower_is_better, 0.0) is False

    # info-only: never breaches
    info_only = slos.SLOS["eval_holdout_growth_weekly"]
    assert slos._is_breach(info_only, 99999.0) is False
