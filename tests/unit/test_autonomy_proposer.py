"""Unit tests for brain_core.autonomy_proposer (Phase 7 closed-loop pipeline).

2026-04-16 rewrite: data source moved from accuracy_tracker (domain-keyed,
never matched autonomy kinds) to action_audit.tool (brain.db, shared
namespace with DEFAULT_LEVELS). Tests updated accordingly.
"""

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
    """Point autonomy_proposer + audit_log at fresh tmp_path DBs."""
    for mod in ("autonomy_proposer", "audit_log", "config", "autonomy"):
        if mod in sys.modules:
            del sys.modules[mod]
    import audit_log
    import autonomy_proposer

    fake_brain_db = tmp_path / "brain.db"
    fake_audit = tmp_path / "audit.db"
    monkeypatch.setattr(autonomy_proposer, "BRAIN_DB", fake_brain_db)
    monkeypatch.setattr(audit_log, "DB_PATH", fake_audit)
    monkeypatch.setattr(audit_log, "_initialized", False, raising=False)

    # Seed action_audit schema
    fake_brain_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(fake_brain_db))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS action_audit ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, route TEXT NOT NULL, "
        "tool TEXT NOT NULL DEFAULT '', actor TEXT NOT NULL DEFAULT 'unknown', "
        "query_text TEXT, retrieved_atom_ids TEXT NOT NULL DEFAULT '[]', "
        "retrieved_chroma_ids TEXT, outcome TEXT, outcome_reason TEXT, "
        "session_id TEXT, created_at TEXT NOT NULL, resolved_at TEXT)"
    )
    conn.commit()
    conn.close()

    yield autonomy_proposer, fake_brain_db, fake_audit
    importlib.reload(autonomy_proposer)


def _seed_outcomes(db_path: Path, kind: str, successes: int, failures: int) -> None:
    """Insert action_audit rows with the given tool/outcome counts."""
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat(timespec="seconds")
    conn = sqlite3.connect(str(db_path))
    for _ in range(successes):
        conn.execute(
            "INSERT INTO action_audit (route, tool, outcome, created_at) VALUES (?, ?, ?, ?)",
            ("/test", kind, "success", now),
        )
    for _ in range(failures):
        conn.execute(
            "INSERT INTO action_audit (route, tool, outcome, created_at) VALUES (?, ?, ?, ?)",
            ("/test", kind, "fail", now),
        )
    conn.commit()
    conn.close()


def test_run_with_no_outcomes_returns_note(isolated_proposer):
    proposer, _, _ = isolated_proposer
    result = proposer.run()
    assert result["promoted_proposals"] == 0
    assert result["demoted_proposals"] == 0
    assert result.get("note") == "no_action_audit_window_data"


def test_skips_when_below_min_outcomes(isolated_proposer, monkeypatch):
    proposer, db, _ = isolated_proposer
    _seed_outcomes(db, "test.kind", successes=10, failures=0)  # below MIN=20
    import autonomy

    monkeypatch.setattr(autonomy, "list_levels", lambda: {"test.kind": "L2"})
    result = proposer.run()
    assert result["promoted_proposals"] == 0


def test_promote_when_high_ratio_at_l2(isolated_proposer, monkeypatch):
    proposer, db, audit_db = isolated_proposer

    import autonomy

    monkeypatch.setattr(autonomy, "list_levels", lambda: {"heal.log_rotate": "L2"})
    _seed_outcomes(db, "heal.log_rotate", successes=25, failures=0)

    result = proposer.run()
    assert result["promoted_proposals"] == 1
    assert result["promotes"][0]["kind"] == "heal.log_rotate"
    assert result["promotes"][0]["target"] == "L3"

    conn = sqlite3.connect(str(audit_db))
    row = conn.execute(
        "SELECT event_type, entity_a, entity_b FROM audit_events " "WHERE event_type='autonomy_proposal'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[1] == "heal.log_rotate"
    assert row[2] == "L2->L3"


def test_promote_l1_to_l2_is_now_supported(isolated_proposer, monkeypatch):
    """2026-04-16 fix: previously L1 kinds could never self-promote."""
    proposer, db, _ = isolated_proposer
    import autonomy

    monkeypatch.setattr(autonomy, "list_levels", lambda: {"brain_loop.observe": "L1"})
    _seed_outcomes(db, "brain_loop.observe", successes=25, failures=0)
    result = proposer.run()
    assert result["promoted_proposals"] == 1
    assert result["promotes"][0]["target"] == "L2"


def test_demote_when_low_ratio_ticks_breaker(isolated_proposer, monkeypatch):
    proposer, db, _ = isolated_proposer

    import autonomy

    monkeypatch.setattr(autonomy, "list_levels", lambda: {"task.dispatch": "L3"})
    _seed_outcomes(db, "task.dispatch", successes=10, failures=15)

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


def test_run_handles_missing_brain_db(isolated_proposer, monkeypatch, tmp_path):
    """If BRAIN_DB doesn't exist, run() returns a note dict, not raises."""
    proposer, _, _ = isolated_proposer
    empty_db = tmp_path / "empty.db"
    monkeypatch.setattr(proposer, "BRAIN_DB", empty_db)
    result = proposer.run()
    assert result.get("note") == "no_action_audit_window_data"
