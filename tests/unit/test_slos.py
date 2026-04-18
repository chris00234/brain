"""Unit tests for brain_core.slos (Phase E1)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


@pytest.fixture
def slos_module():
    if "slos" in sys.modules:
        del sys.modules["slos"]
    import slos

    return slos


def test_slo_count(slos_module):
    # 6 base + 4 N-series watchers + 1 stuck-writer throughput (2026-04-16)
    # + 1 calibration_brier_drift_7d (2026-04-17 W5 — silent miscalibration detector)
    # + 3 incident-response SLOs (2026-04-17: dispatch_failure_rate_1h,
    #   agent_session_max_mb, logs_dir_total_mb)
    assert len(slos_module.SLOS) == 15
    assert "atoms_write_throughput_1h" in slos_module.SLOS
    assert "calibration_brier_drift_7d" in slos_module.SLOS
    assert "dispatch_failure_rate_1h" in slos_module.SLOS
    assert "agent_session_max_mb" in slos_module.SLOS
    assert "logs_dir_total_mb" in slos_module.SLOS


def test_recall_v2_p95_lower_is_better(slos_module):
    slo = slos_module.SLOS["recall_v2_p95_ms"]
    assert slo.target == 500.0
    assert slo.severity == "warning"
    # 600ms > 500 target → breach (latency is lower-is-better)
    assert slos_module._is_breach(slo, 600.0) is True
    assert slos_module._is_breach(slo, 400.0) is False


def test_content_hit_higher_is_better(slos_module):
    slo = slos_module.SLOS["recall_v2_content_hit_pct"]
    assert slo.target == 95.0
    # 90% < 95% target → breach (recall is higher-is-better)
    assert slos_module._is_breach(slo, 90.0) is True
    assert slos_module._is_breach(slo, 96.5) is False


def test_breaker_open_count_zero_target(slos_module):
    slo = slos_module.SLOS["breaker_open_count"]
    assert slo.target == 0.0
    assert slos_module._is_breach(slo, 1.0) is True
    assert slos_module._is_breach(slo, 0.0) is False


def test_eval_holdout_growth_never_breaches(slos_module):
    slo = slos_module.SLOS["eval_holdout_growth_weekly"]
    assert slo.severity == "info"
    assert slos_module._is_breach(slo, 0.0) is False
    assert slos_module._is_breach(slo, 100.0) is False


def test_check_one_returns_result(slos_module, monkeypatch):
    monkeypatch.setitem(slos_module._MEASUREMENTS, "recall_v2_p95_ms", lambda: 150.0)
    result = slos_module.check_one("recall_v2_p95_ms")
    assert result is not None
    assert result.actual == 150.0
    assert result.breached is False
    assert result.delta == 150.0 - 500.0


def test_check_one_unknown_slo(slos_module):
    assert slos_module.check_one("does_not_exist") is None


def test_check_all_returns_all(slos_module, monkeypatch):
    for name in slos_module.SLOS:
        monkeypatch.setitem(slos_module._MEASUREMENTS, name, lambda: 0.0)
    results = slos_module.check_all()
    assert len(results) == len(slos_module.SLOS)


def test_alert_rate_limited(slos_module, monkeypatch):
    """Verify the rate-limit suppresses repeated alerts within the window.

    Stubs the persistent brain_config store with an in-memory dict so the
    test doesn't touch autonomy.db.
    """
    sent: list[str] = []
    fake_store: dict[tuple[str, str], float] = {}
    monkeypatch.setattr(slos_module, "_alert_telegram", lambda slo, actual: sent.append(slo.name) or True)
    monkeypatch.setattr(
        slos_module,
        "_load_last_alert_at",
        lambda name, sev: fake_store.get((name, sev), 0.0),
    )
    monkeypatch.setattr(
        slos_module,
        "_save_last_alert_at",
        lambda name, sev, ts: fake_store.__setitem__((name, sev), ts),
    )
    slo = slos_module.SLOS["breaker_open_count"]
    result = slos_module.SLOResult(slo=slo, actual=2.0, breached=True, delta=2.0)
    assert slos_module.maybe_alert(result) is True
    assert slos_module.maybe_alert(result) is False  # rate-limited
    assert sent == [slo.name]


def test_run_returns_summary(slos_module, monkeypatch):
    for name in slos_module.SLOS:
        monkeypatch.setitem(slos_module._MEASUREMENTS, name, lambda: 0.0)
    fake_store: dict[tuple[str, str], float] = {}
    monkeypatch.setattr(slos_module, "_alert_telegram", lambda slo, actual: True)
    monkeypatch.setattr(
        slos_module,
        "_load_last_alert_at",
        lambda name, sev: fake_store.get((name, sev), 0.0),
    )
    monkeypatch.setattr(
        slos_module,
        "_save_last_alert_at",
        lambda name, sev, ts: fake_store.__setitem__((name, sev), ts),
    )
    summary = slos_module.run()
    assert summary["checked"] == len(slos_module.SLOS)
    assert "results" in summary
    assert len(summary["results"]) == len(slos_module.SLOS)
