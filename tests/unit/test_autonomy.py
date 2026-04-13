"""Unit tests for brain_core.autonomy - L0-L3 gate."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


@pytest.fixture
def isolated_autonomy(tmp_path, monkeypatch):
    """Import autonomy with isolated brain_config DB and autopilot mocked ON."""
    for mod in ("breakers", "autonomy", "config"):
        if mod in sys.modules:
            del sys.modules[mod]
    import autonomy
    import breakers

    fake_db = tmp_path / "autonomy.db"
    monkeypatch.setattr(autonomy, "AUTONOMY_DB", fake_db)
    monkeypatch.setattr(breakers, "AUTONOMY_DB", fake_db)
    monkeypatch.setattr(breakers, "_initialized", False)
    breakers._snapshot_cache.clear()
    autonomy.invalidate_levels_cache()
    monkeypatch.setattr(autonomy, "_autopilot_enabled", lambda: True)
    monkeypatch.delenv("BRAIN_AUTOPILOT_DISABLED", raising=False)
    yield autonomy, breakers
    for mod in ("breakers", "autonomy", "config"):
        if mod in sys.modules:
            del sys.modules[mod]


PT_NIGHT = datetime(2026, 4, 14, 6, 0, tzinfo=ZoneInfo("UTC"))  # 23:00 PT (UTC-7)
PT_MORNING = datetime(2026, 4, 14, 16, 0, tzinfo=ZoneInfo("UTC"))  # 09:00 PT
PT_AFTERNOON = datetime(2026, 4, 14, 21, 0, tzinfo=ZoneInfo("UTC"))  # 14:00 PT
PT_EVENING = datetime(2026, 4, 14, 4, 0, tzinfo=ZoneInfo("UTC"))  # 21:00 PT


def test_default_levels_loaded(isolated_autonomy):
    autonomy, _ = isolated_autonomy
    levels = autonomy.list_levels()
    assert levels.get("heal.log_rotate") == "L3"
    assert levels.get("write.canonical") == "L0"
    assert levels.get("goal.decompose") == "L1"


def test_env_kill_blocks_everything(isolated_autonomy, monkeypatch):
    autonomy, _ = isolated_autonomy
    monkeypatch.setenv("BRAIN_AUTOPILOT_DISABLED", "1")
    d = autonomy.authorize("heal.log_rotate")
    assert d.allowed is False
    assert d.reason == "env_kill"


def test_autopilot_off_blocks_everything(isolated_autonomy, monkeypatch):
    autonomy, _ = isolated_autonomy
    monkeypatch.setattr(autonomy, "_autopilot_enabled", lambda: False)
    d = autonomy.authorize("heal.log_rotate")
    assert d.allowed is False
    assert d.reason == "autopilot_off"


def test_denylist_blocks_cloudflared(isolated_autonomy):
    autonomy, _ = isolated_autonomy
    d = autonomy.authorize("cloudflared.restart")
    assert d.allowed is False
    assert d.reason == "denylist"


def test_l3_runs_immediately_in_quiet_hours_for_exception(isolated_autonomy):
    autonomy, _ = isolated_autonomy
    # PT_EVENING = 21:00 PT, NOT in quiet hours (23-07)
    d = autonomy.authorize("heal.log_rotate", now=PT_EVENING)
    assert d.allowed is True
    assert d.level == "L3"


def test_quiet_hours_demotes_l3_to_l2(isolated_autonomy):
    autonomy, _ = isolated_autonomy
    d = autonomy.authorize("llm.dispatch", now=PT_NIGHT)
    assert d.allowed is True
    assert d.level == "L2"  # demoted from L3
    assert d.notify_lag_s > 0


def test_quiet_hours_exception_keeps_l3(isolated_autonomy):
    autonomy, _ = isolated_autonomy
    d = autonomy.authorize("heal.log_rotate", now=PT_NIGHT)
    assert d.level == "L3", "log_rotate is in QUIET_HOURS exceptions"


def test_work_hours_blocks_night_only_kind(isolated_autonomy):
    autonomy, _ = isolated_autonomy
    # heal.reindex is night-only, PT_AFTERNOON is 14:00 PT (work block)
    d = autonomy.authorize("heal.reindex", now=PT_AFTERNOON)
    assert d.allowed is False
    assert "execution_window_block" in d.reason


def test_l1_propose_only(isolated_autonomy):
    autonomy, _ = isolated_autonomy
    d = autonomy.authorize("goal.decompose", now=PT_MORNING)
    assert d.allowed is True
    assert d.level == "L1"
    assert d.requires_ack is True
    assert d.notify_lag_s == 0


def test_l2_notify_then_act(isolated_autonomy):
    autonomy, _ = isolated_autonomy
    d = autonomy.authorize("task.dispatch", now=PT_MORNING)
    assert d.allowed is True
    assert d.level == "L2"
    assert d.requires_ack is False
    assert d.notify_lag_s > 0


def test_l3_immediate(isolated_autonomy):
    autonomy, _ = isolated_autonomy
    d = autonomy.authorize("llm.dispatch", now=PT_MORNING)
    assert d.allowed is True
    assert d.level == "L3"


def test_open_breaker_blocks(isolated_autonomy):
    autonomy, breakers = isolated_autonomy
    for _ in range(3):
        breakers.record_result("llm.dispatch", ok=False)
    d = autonomy.authorize("llm.dispatch", now=PT_MORNING)
    assert d.allowed is False
    assert d.reason == "breaker_open"
    assert d.breaker_state == "open"


def test_set_level_overrides_default(isolated_autonomy):
    autonomy, _ = isolated_autonomy
    autonomy.set_level("heal.reindex", "L1", updated_by="test")
    levels = autonomy.list_levels()
    assert levels["heal.reindex"] == "L1"


def test_unknown_kind_falls_back_to_family(isolated_autonomy):
    autonomy, _ = isolated_autonomy
    d = autonomy.authorize("trigger.fire.health_check_failed", now=PT_MORNING)
    assert d.level == "L2"  # falls back to "trigger.fire" family default


def test_unknown_kind_unknown_family_defaults_to_l1(isolated_autonomy):
    autonomy, _ = isolated_autonomy
    d = autonomy.authorize("totally.new.action", now=PT_MORNING)
    assert d.level == "L1"
    assert d.requires_ack is True
