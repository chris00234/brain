"""Unit tests for brain_core.schema_versions migration runner.

Uses isolated_schema_versions fixture so we never touch the production DB.
"""

from __future__ import annotations

import pytest


def test_baseline_version_is_zero(isolated_schema_versions):
    sv = isolated_schema_versions
    assert sv.get_version("test_component") == 0


def test_set_and_get_version_roundtrip(isolated_schema_versions):
    sv = isolated_schema_versions
    sv.set_version("test_component", 3)
    assert sv.get_version("test_component") == 3


def test_set_version_is_upsert(isolated_schema_versions):
    sv = isolated_schema_versions
    sv.set_version("test_component", 1)
    sv.set_version("test_component", 2)
    sv.set_version("test_component", 3)
    assert sv.get_version("test_component") == 3


def test_migration_decorator_registers(isolated_schema_versions):
    sv = isolated_schema_versions
    calls = []

    @sv.migration("xyzzy", 0, 1)
    def m():
        calls.append(1)
        return {"created": "x"}

    assert ("xyzzy", 0, 1) in sv.MIGRATIONS
    ok, detail = sv._run_one_migration("xyzzy", 0, 1)
    assert ok is True
    assert calls == [1]
    assert sv.get_version("xyzzy") == 1


def test_migration_failure_does_not_advance_version(isolated_schema_versions):
    sv = isolated_schema_versions

    @sv.migration("boom", 0, 1)
    def m():
        raise RuntimeError("boom!")

    sv.set_version("boom", 0)
    ok, detail = sv._run_one_migration("boom", 0, 1)
    assert ok is False
    assert "boom!" in detail
    assert sv.get_version("boom") == 0


def test_unregistered_migration_treated_as_noop(isolated_schema_versions):
    sv = isolated_schema_versions
    sv.set_version("baseline_only", 0)
    ok, detail = sv._run_one_migration("baseline_only", 0, 1)
    assert ok is True
    assert "no_migration_registered" in detail
    assert sv.get_version("baseline_only") == 1


def test_check_and_migrate_refuses_downgrade(isolated_schema_versions, monkeypatch):
    sv = isolated_schema_versions
    sv.set_version("downgrader", 5)
    monkeypatch.setattr(sv, "CURRENT_VERSIONS", {"downgrader": 3})
    with pytest.raises(RuntimeError, match="downgrade refused"):
        sv.check_and_migrate()


def test_check_and_migrate_runs_chain(isolated_schema_versions, monkeypatch):
    sv = isolated_schema_versions
    calls = []

    @sv.migration("chain", 0, 1)
    def _01():
        calls.append((0, 1))
        return {}

    @sv.migration("chain", 1, 2)
    def _12():
        calls.append((1, 2))
        return {}

    @sv.migration("chain", 2, 3)
    def _23():
        calls.append((2, 3))
        return {}

    monkeypatch.setattr(sv, "CURRENT_VERSIONS", {"chain": 3})
    result = sv.check_and_migrate()
    assert calls == [(0, 1), (1, 2), (2, 3)]
    assert sv.get_version("chain") == 3
    assert len([m for m in result["migrated"] if m.startswith("chain")]) == 3
