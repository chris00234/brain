"""Unit tests for brain_core.action_triggers CRUD helpers (Phase B1)."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


@pytest.fixture
def isolated_triggers(tmp_path, monkeypatch):
    """Point action_triggers at a tmp_path autonomy.db."""
    if "action_triggers" in sys.modules:
        del sys.modules["action_triggers"]
    if "config" in sys.modules:
        del sys.modules["config"]
    import action_triggers

    fake_db = tmp_path / "autonomy.db"
    monkeypatch.setattr(action_triggers, "DB_PATH", fake_db)
    yield action_triggers
    importlib.reload(action_triggers)


def _sample_trigger(name: str = "test_trigger"):
    return {
        "name": name,
        "description": "test description",
        "condition_type": "proactive_insight",
        "condition_config": {"category": "test", "severity": "info"},
        "action_template": {"task_template": "echo test"},
        "enabled": True,
        "cooldown_seconds": 600,
    }


def test_create_returns_full_row(isolated_triggers):
    result = isolated_triggers.create_trigger(**_sample_trigger())
    assert result["name"] == "test_trigger"
    assert result["enabled"] == 1
    assert result["cooldown_seconds"] == 600
    assert "id" in result and len(result["id"]) > 0


def test_create_duplicate_name_raises(isolated_triggers):
    isolated_triggers.create_trigger(**_sample_trigger())
    with pytest.raises(ValueError, match="already exists"):
        isolated_triggers.create_trigger(**_sample_trigger())


def test_get_trigger_returns_none_for_missing(isolated_triggers):
    assert isolated_triggers.get_trigger("nope") is None


def test_get_trigger_after_create(isolated_triggers):
    created = isolated_triggers.create_trigger(**_sample_trigger())
    fetched = isolated_triggers.get_trigger(created["id"])
    assert fetched is not None
    assert fetched["name"] == "test_trigger"


def test_update_partial_fields(isolated_triggers):
    created = isolated_triggers.create_trigger(**_sample_trigger())
    updated = isolated_triggers.update_trigger(
        created["id"],
        enabled=False,
        cooldown_seconds=7200,
    )
    assert updated is not None
    assert updated["enabled"] == 0
    assert updated["cooldown_seconds"] == 7200
    assert updated["description"] == "test description"  # unchanged


def test_update_missing_returns_none(isolated_triggers):
    result = isolated_triggers.update_trigger("missing_id", enabled=False)
    assert result is None


def test_update_no_fields_is_noop(isolated_triggers):
    created = isolated_triggers.create_trigger(**_sample_trigger())
    result = isolated_triggers.update_trigger(created["id"])
    assert result is not None
    assert result["enabled"] == 1


def test_delete_existing(isolated_triggers):
    created = isolated_triggers.create_trigger(**_sample_trigger())
    assert isolated_triggers.delete_trigger(created["id"]) is True
    assert isolated_triggers.get_trigger(created["id"]) is None


def test_delete_missing_returns_false(isolated_triggers):
    assert isolated_triggers.delete_trigger("missing") is False


def test_list_includes_created(isolated_triggers):
    isolated_triggers.create_trigger(**_sample_trigger("alpha"))
    isolated_triggers.create_trigger(**_sample_trigger("beta"))
    rows = isolated_triggers.list_triggers()
    names = {r["name"] for r in rows}
    assert "alpha" in names and "beta" in names
