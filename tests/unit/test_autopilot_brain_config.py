"""Unit tests for brain_core.autopilot brain_config migration (Phase F2)."""

from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


@pytest.fixture
def isolated_autopilot(tmp_path, monkeypatch):
    """Point autopilot at tmp_path autonomy.db and JSON state file."""
    if "autopilot" in sys.modules:
        del sys.modules["autopilot"]
    if "config" in sys.modules:
        del sys.modules["config"]
    import autopilot

    fake_db = tmp_path / "autonomy.db"
    fake_json = tmp_path / "autopilot_state.json"
    monkeypatch.setattr(autopilot, "AUTONOMY_DB", fake_db)
    monkeypatch.setattr(autopilot, "STATE_FILE", fake_json)
    yield autopilot
    importlib.reload(autopilot)


def _read_brain_config(db_path: Path) -> dict:
    if not db_path.exists():
        return {}
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT key, value FROM brain_config").fetchall()
    finally:
        conn.close()
    return dict(rows)


def test_get_state_defaults_when_empty(isolated_autopilot):
    state = isolated_autopilot.get_state()
    assert state["enabled"] is False
    assert state["confidence_threshold"] == 0.8


def test_set_state_persists_to_brain_config(isolated_autopilot):
    isolated_autopilot.set_state(enabled=True, threshold=0.75, updated_by="test")
    cfg = _read_brain_config(isolated_autopilot.AUTONOMY_DB)
    assert cfg.get("autopilot.enabled") == "true"
    assert cfg.get("autopilot.confidence_threshold") == "0.75"


def test_get_state_after_set(isolated_autopilot):
    isolated_autopilot.set_state(enabled=True, threshold=0.6)
    state = isolated_autopilot.get_state()
    assert state["enabled"] is True
    assert state["confidence_threshold"] == 0.6


def test_json_migration_seeds_brain_config(isolated_autopilot, tmp_path):
    legacy = {
        "enabled": True,
        "confidence_threshold": 0.9,
        "updated_at": "2025-01-01T00:00:00+00:00",
        "updated_by": "legacy_test",
    }
    isolated_autopilot.STATE_FILE.write_text(json.dumps(legacy))
    state = isolated_autopilot.get_state()
    assert state["enabled"] is True
    assert state["confidence_threshold"] == 0.9
    cfg = _read_brain_config(isolated_autopilot.AUTONOMY_DB)
    assert cfg.get("autopilot.enabled") == "true"
    assert cfg.get("autopilot.confidence_threshold") == "0.9"


def test_json_migration_runs_only_once(isolated_autopilot):
    isolated_autopilot.STATE_FILE.write_text(json.dumps({"enabled": True, "confidence_threshold": 0.85}))
    isolated_autopilot.get_state()
    isolated_autopilot.set_state(enabled=False, threshold=0.7, updated_by="post_migration")
    isolated_autopilot.STATE_FILE.write_text(json.dumps({"enabled": True, "confidence_threshold": 0.99}))
    state = isolated_autopilot.get_state()
    assert state["enabled"] is False
    assert state["confidence_threshold"] == 0.7


def test_is_enabled_shortcut(isolated_autopilot):
    isolated_autopilot.set_state(enabled=True, threshold=0.8)
    assert isolated_autopilot.is_enabled() is True
    isolated_autopilot.set_state(enabled=False, threshold=0.8)
    assert isolated_autopilot.is_enabled() is False


def test_should_auto_approve(isolated_autopilot):
    isolated_autopilot.set_state(enabled=True, threshold=0.8)
    assert isolated_autopilot.should_auto_approve(0.9) is True
    assert isolated_autopilot.should_auto_approve(0.7) is False
    isolated_autopilot.set_state(enabled=False, threshold=0.8)
    assert isolated_autopilot.should_auto_approve(0.9) is False
