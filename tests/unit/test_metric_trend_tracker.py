"""tests/unit/test_metric_trend_tracker.py — 7d drift alerts.

Stubs `brain_config_store` and `_build_snapshot` so the tests don't
touch live state. Locks the bad-direction detection rule and the
baseline-window matching.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "brain_core"))

import metric_trend_tracker  # noqa: E402


def _iso(hours_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


class _FakeStore:
    def __init__(self, state: dict[str, str] | None = None) -> None:
        self._state: dict[str, str] = state or {}

    def get(self, key: str) -> str | None:
        return self._state.get(key)

    def set(self, key: str, value: str, **_) -> None:
        self._state[key] = value


@pytest.fixture
def fake_store(monkeypatch):
    store = _FakeStore()
    monkeypatch.setitem(sys.modules, "brain_config_store", SimpleNamespace(get=store.get, set=store.set))
    return store


def test_snapshot_now_appends_history(monkeypatch, fake_store):
    monkeypatch.setattr(
        metric_trend_tracker,
        "_build_snapshot",
        lambda: {"override_pct.coding": 90.0, "slo.breached_count": 0.0},
    )
    summary = metric_trend_tracker.snapshot_now()
    assert summary["status"] == "ok"
    assert summary["metrics"] == 2

    raw = fake_store.get(metric_trend_tracker.HISTORY_KEY)
    hist = json.loads(raw)
    assert len(hist) == 1
    assert hist[0]["snapshot"]["override_pct.coding"] == 90.0


def test_compute_trend_alerts_detects_bad_direction(monkeypatch, fake_store):
    history = [
        {"ts": _iso(168), "snapshot": {"override_pct.coding": 70.0}},
        {"ts": _iso(0), "snapshot": {"override_pct.coding": 90.0}},
    ]
    fake_store.set(metric_trend_tracker.HISTORY_KEY, json.dumps(history))
    alerts = metric_trend_tracker.compute_trend_alerts()
    assert len(alerts) == 1
    a = alerts[0]
    assert a["metric"] == "override_pct.coding"
    assert a["delta"] == 20.0
    assert a["lower_is_better"] is True


def test_compute_trend_alerts_ignores_improvement(monkeypatch, fake_store):
    history = [
        {"ts": _iso(168), "snapshot": {"override_pct.coding": 90.0}},
        {"ts": _iso(0), "snapshot": {"override_pct.coding": 60.0}},
    ]
    fake_store.set(metric_trend_tracker.HISTORY_KEY, json.dumps(history))
    alerts = metric_trend_tracker.compute_trend_alerts()
    assert alerts == []


def test_compute_trend_alerts_requires_baseline_window(monkeypatch, fake_store):
    """No 7d-prior baseline → no alerts. Avoids alerting on cold-start state."""
    history = [
        {"ts": _iso(1), "snapshot": {"override_pct.coding": 60.0}},
        {"ts": _iso(0), "snapshot": {"override_pct.coding": 90.0}},
    ]
    fake_store.set(metric_trend_tracker.HISTORY_KEY, json.dumps(history))
    alerts = metric_trend_tracker.compute_trend_alerts()
    assert alerts == []


def test_compute_trend_alerts_abs_threshold(monkeypatch, fake_store):
    """slo.breached_count uses an absolute threshold (drift_abs_alert=1)."""
    history = [
        {"ts": _iso(168), "snapshot": {"slo.breached_count": 0.0}},
        {"ts": _iso(0), "snapshot": {"slo.breached_count": 2.0}},
    ]
    fake_store.set(metric_trend_tracker.HISTORY_KEY, json.dumps(history))
    alerts = metric_trend_tracker.compute_trend_alerts()
    assert any(a["metric"] == "slo.breached_count" for a in alerts)
