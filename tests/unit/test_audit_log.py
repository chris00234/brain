"""Unit tests for brain_core.audit_log — dedup/merge/conflict event log."""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def isolated_audit(tmp_path, monkeypatch):
    """Point audit_log at a fresh tmp_path DB so we never touch logs/audit.db."""
    import audit_log

    fake_db = tmp_path / "audit.db"
    monkeypatch.setattr(audit_log, "DB_PATH", fake_db)
    monkeypatch.setattr(audit_log, "_schema_initialized", False)
    yield audit_log
    importlib.reload(audit_log)


def test_log_event_returns_id(isolated_audit):
    eid = isolated_audit.log_event(event_type="test", entity_a="A", entity_b="B")
    assert eid.startswith("audit_")
    assert len(eid) > 6


def test_log_event_persists_to_db(isolated_audit):
    isolated_audit.log_event(
        event_type="merge",
        entity_a="atm_001",
        entity_b="atm_002",
        match_score=0.92,
        resolution="merged_into_a",
        reason="duplicate content",
    )
    rows = isolated_audit.list_events(event_type="merge", limit=10)
    assert len(rows) == 1
    assert rows[0]["entity_a"] == "atm_001"
    assert rows[0]["entity_b"] == "atm_002"
    assert abs(rows[0]["match_score"] - 0.92) < 1e-9
    assert rows[0]["resolution"] == "merged_into_a"


def test_review_required_sets_pending(isolated_audit):
    isolated_audit.log_event(
        event_type="conflict",
        entity_a="x",
        entity_b="y",
        review_required=True,
    )
    pending = isolated_audit.list_events(pending_only=True)
    assert len(pending) == 1
    assert pending[0]["review_required"] == 1
    assert pending[0]["reviewed_at"] is None


def test_event_type_filter(isolated_audit):
    isolated_audit.log_event(event_type="merge", entity_a="a")
    isolated_audit.log_event(event_type="conflict", entity_a="b")
    isolated_audit.log_event(event_type="merge", entity_a="c")
    merges = isolated_audit.list_events(event_type="merge")
    conflicts = isolated_audit.list_events(event_type="conflict")
    assert len(merges) == 2
    assert len(conflicts) == 1
