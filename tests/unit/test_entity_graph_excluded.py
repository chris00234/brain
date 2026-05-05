"""Phase G3 — get_excluded_entities cache + Cypher fallback paths."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


@pytest.fixture
def fresh_eg(monkeypatch):
    """Reload entity_graph each test so the module-level _excluded_cache starts empty."""
    if "entity_graph" in sys.modules:
        del sys.modules["entity_graph"]
    import entity_graph

    yield entity_graph
    if "entity_graph" in sys.modules:
        del sys.modules["entity_graph"]


def test_no_neo4j_returns_empty_set(fresh_eg, monkeypatch):
    """Neo4j unavailable → empty set, no exception, no Cypher attempt."""
    called = {"run_query": 0}

    monkeypatch.setattr(fresh_eg, "_use_neo4j", lambda: False)

    def _fail_run(*_a, **_kw):
        called["run_query"] += 1
        raise AssertionError("run_query must not be called when Neo4j is down")

    monkeypatch.setattr("neo4j_client.run_query", _fail_run, raising=False)
    out = fresh_eg.get_excluded_entities("chris", "uses")
    assert out == set()
    assert called["run_query"] == 0


def test_returns_lowercased_distinct_names(fresh_eg, monkeypatch):
    """Cypher rows → lowercased name set; preserves distinct semantics."""
    monkeypatch.setattr(fresh_eg, "_use_neo4j", lambda: True)

    fake_rows = [
        {"name": "beszel"},
        {"name": "glance"},
        {"name": "uptime kuma"},
        {"name": None},  # Cypher can return null; must be filtered
    ]
    monkeypatch.setattr("neo4j_client.run_query", lambda *_a, **_kw: fake_rows)

    out = fresh_eg.get_excluded_entities("chris", "uses")
    assert out == {"beszel", "glance", "uptime kuma"}


def test_cache_short_circuits_second_call(fresh_eg, monkeypatch):
    """Second call within TTL must NOT hit Neo4j again."""
    monkeypatch.setattr(fresh_eg, "_use_neo4j", lambda: True)

    call_count = {"n": 0}

    def _counted_run(*_a, **_kw):
        call_count["n"] += 1
        return [{"name": "beszel"}]

    monkeypatch.setattr("neo4j_client.run_query", _counted_run)

    a = fresh_eg.get_excluded_entities("chris", "uses")
    b = fresh_eg.get_excluded_entities("chris", "uses")
    assert a == b == {"beszel"}
    assert call_count["n"] == 1, "cache must absorb second call within TTL"


def test_query_failure_returns_empty(fresh_eg, monkeypatch):
    """Cypher raising → empty set, never propagates (filter is opt-in / best effort)."""
    monkeypatch.setattr(fresh_eg, "_use_neo4j", lambda: True)

    def _boom(*_a, **_kw):
        raise RuntimeError("neo4j down")

    monkeypatch.setattr("neo4j_client.run_query", _boom)

    out = fresh_eg.get_excluded_entities("chris", "uses")
    assert out == set()


def test_empty_cache_key_short_circuits(fresh_eg, monkeypatch):
    """speaker='' AND relationship='' → empty key, no Cypher attempt."""

    def _fail_run(*_a, **_kw):
        raise AssertionError("run_query must not be called for empty key")

    monkeypatch.setattr("neo4j_client.run_query", _fail_run, raising=False)
    out = fresh_eg.get_excluded_entities("", "")
    assert out == set()
