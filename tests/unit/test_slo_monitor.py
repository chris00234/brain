from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "brain_core"))

import slo_monitor  # noqa: E402


def test_recall_v2_monitor_uses_1000ms_floor(monkeypatch):
    monkeypatch.setattr(
        slo_monitor,
        "probe",
        lambda: {
            "recall": {"samples": 5, "p95": 900.0, "mean": 400.0},
            "recall_v2": {"samples": 3, "p95": 900.0, "mean": 400.0},
        },
    )
    monkeypatch.setattr(
        slo_monitor,
        "load_baseline",
        lambda: {
            "recall_p95_ms": 100.0,
            "recall_v2_p95_ms": 100.0,
            "memory_growth_weekly_pct": 20,
        },
    )

    result = slo_monitor.check_slos()

    assert result["status"] == "ok"
    assert result["violations"] == []


def test_recall_v2_monitor_breaches_above_1000ms_floor(monkeypatch):
    monkeypatch.setattr(
        slo_monitor,
        "probe",
        lambda: {
            "recall": {"samples": 5, "p95": 1001.0, "mean": 400.0},
            "recall_v2": {"samples": 3, "p95": 1001.0, "mean": 400.0},
        },
    )
    monkeypatch.setattr(
        slo_monitor,
        "load_baseline",
        lambda: {
            "recall_p95_ms": 100.0,
            "recall_v2_p95_ms": 100.0,
            "memory_growth_weekly_pct": 20,
        },
    )

    result = slo_monitor.check_slos()

    assert result["status"] == "breached"
    assert {v["slo"] for v in result["violations"]} == {
        "recall_p95_ms",
        "recall_v2_p95_ms",
    }
    assert {v["threshold"] for v in result["violations"]} == {1000}
