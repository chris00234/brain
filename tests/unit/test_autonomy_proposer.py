"""Unit tests for brain_core.autonomy_proposer (Phase 7 closed-loop pipeline)."""

from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


@pytest.fixture
def isolated_proposer(tmp_path, monkeypatch):
    """Point autonomy_proposer + audit_log at a fresh tmp_path autonomy/audit DBs."""
    for mod in ("autonomy_proposer", "audit_log", "config", "autonomy"):
        if mod in sys.modules:
            del sys.modules[mod]
    import audit_log
    import autonomy_proposer

    fake_db = tmp_path / "autonomy.db"
    fake_audit = tmp_path / "audit.db"
    monkeypatch.setattr(autonomy_proposer, "AUTONOMY_DB", fake_db)
    monkeypatch.setattr(audit_log, "DB_PATH", fake_audit)
    monkeypatch.setattr(audit_log, "_initialized", False, raising=False)

    # Seed accuracy_tracker schema + rows
    fake_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(fake_db))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS accuracy_tracker ("
        "domain TEXT PRIMARY KEY, total_recommendations INTEGER, "
        "correct_recommendations INTEGER, override_count INTEGER)"
    )
    conn.commit()
    conn.close()

    yield autonomy_proposer, fake_db, fake_audit
    importlib.reload(autonomy_proposer)


def _seed_outcome(db_path: Path, kind: str, total: int, correct: int, overrides: int = 0) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT OR REPLACE INTO accuracy_tracker "
        "(domain, total_recommendations, correct_recommendations, override_count) "
        "VALUES (?, ?, ?, ?)",
        (kind, total, correct, overrides),
    )
    conn.commit()
    conn.close()


def test_run_with_no_outcomes_returns_empty(isolated_proposer):
    proposer, _, _ = isolated_proposer
    result = proposer.run()
    assert result["promoted_proposals"] == 0
    assert result["demoted_proposals"] == 0
    assert result["skipped"] == 0


def test_skips_when_below_min_outcomes(isolated_proposer, monkeypatch):
    proposer, db, _ = isolated_proposer
    _seed_outcome(db, "test.kind", total=10, correct=10)  # below MIN=20
    monkeypatch.setattr(proposer, "list_levels", lambda: {"test.kind": "L2"}, raising=False)
    result = proposer.run()
    assert result["promoted_proposals"] == 0


def test_promote_when_high_ratio_at_l2(isolated_proposer, monkeypatch):
    proposer, db, audit_db = isolated_proposer

    # Patch list_levels to return L2 for our target kind
    import autonomy

    monkeypatch.setattr(autonomy, "list_levels", lambda: {"heal.log_rotate": "L2"})
    _seed_outcome(db, "heal.log_rotate", total=25, correct=25)

    result = proposer.run()
    assert result["promoted_proposals"] == 1
    assert result["promotes"][0]["kind"] == "heal.log_rotate"

    # Verify audit_events row was written
    conn = sqlite3.connect(str(audit_db))
    row = conn.execute(
        "SELECT event_type, entity_a, entity_b FROM audit_events " "WHERE event_type='autonomy_proposal'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[1] == "heal.log_rotate"
    assert row[2] == "L2->L3"


def test_demote_when_low_ratio_ticks_breaker(isolated_proposer, monkeypatch):
    proposer, db, _ = isolated_proposer

    import autonomy

    monkeypatch.setattr(autonomy, "list_levels", lambda: {"task.dispatch": "L3"})
    _seed_outcome(db, "task.dispatch", total=25, correct=10, overrides=15)

    # Stub the breaker tick so we don't need a real breakers DB
    breaker_calls: list[str] = []

    import breakers

    monkeypatch.setattr(
        breakers,
        "record_result",
        lambda kind, **kw: breaker_calls.append(kind),
    )

    result = proposer.run()
    assert result["demoted_proposals"] == 1
    assert result["demotes"][0]["kind"] == "task.dispatch"
    assert result["demotes"][0]["target"] == "L2"
    assert "task.dispatch" in breaker_calls


def test_run_handles_missing_table(isolated_proposer, monkeypatch, tmp_path):
    """If accuracy_tracker doesn't exist, run() returns an error dict, not raises."""
    proposer, _, _ = isolated_proposer
    # Point at a fresh DB without accuracy_tracker
    empty_db = tmp_path / "empty.db"
    monkeypatch.setattr(proposer, "AUTONOMY_DB", empty_db)
    result = proposer.run()
    assert "error" in result
