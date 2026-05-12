"""Unit tests for brain_core.fact_store.

fact_store had no existing test coverage. After the 2026-05-12 migration to
db.open_facts_db utilities, these tests pin the (entity, attribute, value)
triple store behavior including dedup, supersession, and confidence bump.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


@pytest.fixture
def isolated_facts(tmp_path, monkeypatch):
    """Point fact_store at a fresh tmp_path facts.db.

    db.open_facts_db reads from db.FACTS_DB; monkeypatch + clear schema cache.
    """
    for mod in ("fact_store", "audit_log", "config", "db"):
        if mod in sys.modules:
            del sys.modules[mod]
    import db as _db
    import fact_store

    fake_facts = tmp_path / "facts.db"
    fake_audit = tmp_path / "audit.db"
    monkeypatch.setattr(_db, "FACTS_DB", fake_facts)
    monkeypatch.setattr(_db, "AUDIT_DB", fake_audit)  # store_fact writes audit
    monkeypatch.delenv("BRAIN_AUDIT_DB", raising=False)
    _db._schema_cache.clear()
    yield fact_store


def test_store_fact_creates_active_row(isolated_facts):
    result = isolated_facts.store_fact(
        "chris cho",
        "location",
        "Irvine, California",
        source="test",
        confidence=0.95,
    )
    assert result["status"] == "created"
    assert result["id"].startswith("fact_")

    facts = isolated_facts.query_facts(entity="chris cho")
    assert len(facts) == 1
    assert facts[0]["value"] == "Irvine, California"
    assert facts[0]["status"] == "active"
    assert abs(facts[0]["confidence"] - 0.95) < 1e-9


def test_store_fact_same_value_bumps_confidence(isolated_facts):
    """Re-storing same (entity, attribute, normalized_value) updates confidence
    instead of creating a new row."""
    r1 = isolated_facts.store_fact("chris", "city", "Irvine", confidence=0.5)
    r2 = isolated_facts.store_fact("chris", "city", "Irvine", confidence=0.9)
    assert r1["status"] == "created"
    assert r2["status"] == "updated"
    assert r2["action"] == "confidence_bump"
    assert r2["id"] == r1["id"]

    facts = isolated_facts.query_facts(entity="chris", attribute="city")
    assert len(facts) == 1
    assert abs(facts[0]["confidence"] - 0.9) < 1e-9


def test_store_fact_higher_confidence_supersedes_lower(isolated_facts):
    """Different value with equal-or-higher confidence supersedes the old."""
    isolated_facts.store_fact("chris", "city", "Irvine", confidence=0.7)
    r2 = isolated_facts.store_fact("chris", "city", "Newport Beach", confidence=0.9)
    assert r2["status"] == "superseded"

    active = isolated_facts.query_facts(entity="chris", active_only=True)
    history = isolated_facts.get_fact_history("chris", "city")
    assert len(active) == 1
    assert active[0]["value"] == "Newport Beach"
    assert len(history) == 2
    statuses = {h["status"] for h in history}
    assert statuses == {"active", "superseded"}


def test_store_fact_lower_confidence_does_not_supersede(isolated_facts):
    """Lower-confidence write keeps existing active row + inserts a low row."""
    isolated_facts.store_fact("chris", "city", "Irvine", confidence=0.9)
    r2 = isolated_facts.store_fact("chris", "city", "LA", confidence=0.3)
    assert r2["status"] == "superseded_by_existing"

    active = isolated_facts.query_facts(entity="chris", attribute="city", active_only=True)
    assert len(active) == 1
    assert active[0]["value"] == "Irvine"


def test_query_facts_attribute_filter(isolated_facts):
    isolated_facts.store_fact("chris", "city", "Irvine")
    isolated_facts.store_fact("chris", "framework", "FastAPI")
    isolated_facts.store_fact("alice", "city", "Seattle")

    city_only = isolated_facts.query_facts(attribute="city")
    fw_only = isolated_facts.query_facts(attribute="framework")
    assert {f["entity"] for f in city_only} == {"chris", "alice"}
    assert {f["entity"] for f in fw_only} == {"chris"}


def test_get_entity_facts_returns_all_active(isolated_facts):
    isolated_facts.store_fact("chris", "city", "Irvine")
    isolated_facts.store_fact("chris", "framework", "FastAPI")
    isolated_facts.store_fact("chris", "lang", "Python")

    facts = isolated_facts.get_entity_facts("chris")
    attrs = {f["attribute"] for f in facts}
    assert attrs == {"city", "framework", "lang"}


def test_stats_summary(isolated_facts):
    isolated_facts.store_fact("chris", "city", "Irvine", confidence=0.7)
    isolated_facts.store_fact("chris", "city", "Newport Beach", confidence=0.9)
    isolated_facts.store_fact("alice", "city", "Seattle")

    s = isolated_facts.stats()
    assert s["total_facts"] == 3
    assert s["active_facts"] == 2
    assert s["superseded_facts"] == 1
    assert s["unique_entities"] == 2
    assert s["unique_attributes"] == 1


def test_isolation_level_none_enables_manual_transactions(isolated_facts):
    """Regression guard for the post-migration isolation_level=None setup.
    Without it, the explicit BEGIN IMMEDIATE inside _conn_ctx would conflict
    with python's auto-BEGIN and raise OperationalError."""
    # If isolation level wasn't set right, the very first store_fact call
    # would raise sqlite3.OperationalError("cannot start a transaction within
    # a transaction"). The fact that any earlier test passed proves it's OK,
    # but pin it explicitly:
    try:
        isolated_facts.store_fact("test", "guard", "ok", confidence=0.5)
    except sqlite3.OperationalError as exc:
        pytest.fail(f"transaction nesting regressed: {exc}")
