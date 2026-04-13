"""Unit tests for cli.eval_gate two-track behavior."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "cli"))


@pytest.fixture
def fake_eval_gate(tmp_path, monkeypatch):
    """Import eval_gate with paths pointing into tmp_path."""
    import eval_gate

    monkeypatch.setattr(eval_gate, "BRAIN_ROOT", tmp_path)
    monkeypatch.setattr(eval_gate, "EVAL_COMPARE", tmp_path / "eval_compare.py")
    monkeypatch.setattr(eval_gate, "DEFAULT_EVAL_SET", tmp_path / "eval_set.json")
    monkeypatch.setattr(eval_gate, "DEFAULT_BASELINE", tmp_path / "eval_baseline.json")
    (tmp_path / "logs").mkdir()
    yield eval_gate
    importlib.reload(eval_gate)


def _stub_report(content: float, source: float = 80.0, total: int = 100) -> dict:
    return {
        "cases": total,
        "v2": {
            "total": total,
            "hit_content_pct": content,
            "hit_source_pct": source,
            "mean_rank": 1.5,
            "mean_latency_ms": 300,
        },
    }


def test_persist_default_track_uses_legacy_paths(fake_eval_gate, tmp_path):
    fake_eval_gate._persist_eval_report(_stub_report(95.7), track="default")
    assert (tmp_path / "logs" / "eval-report.json").exists()
    assert (tmp_path / "logs" / "eval-history.jsonl").exists()


def test_persist_named_track_uses_suffixed_paths(fake_eval_gate, tmp_path):
    fake_eval_gate._persist_eval_report(_stub_report(95.7), track="stable")
    assert (tmp_path / "logs" / "eval-report-stable.json").exists()
    assert (tmp_path / "logs" / "eval-history-stable.jsonl").exists()
    # Default-track files should NOT exist
    assert not (tmp_path / "logs" / "eval-report.json").exists()


def test_baseline_roundtrip(fake_eval_gate, tmp_path):
    baseline = tmp_path / "baseline.json"
    fake_eval_gate.write_baseline(_stub_report(95.7), baseline)
    loaded = fake_eval_gate.load_baseline(baseline)
    assert loaded is not None
    assert loaded["v2"]["hit_content_pct"] == 95.7
    assert "baseline_written_at" in loaded


def test_load_baseline_missing_returns_none(fake_eval_gate, tmp_path):
    assert fake_eval_gate.load_baseline(tmp_path / "no_such.json") is None


def test_main_bootstrap_writes_baseline(fake_eval_gate, tmp_path, monkeypatch):
    monkeypatch.setattr(fake_eval_gate, "run_current_eval", lambda p: _stub_report(95.7))
    eval_set = tmp_path / "eval_set_stable.json"
    eval_set.write_text("[]")
    baseline = tmp_path / "eval_baseline_stable.json"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "eval_gate",
            "--eval-set",
            str(eval_set),
            "--baseline",
            str(baseline),
            "--track",
            "stable",
        ],
    )
    rc = fake_eval_gate.main()
    assert rc == 0
    assert baseline.exists()
    loaded = json.loads(baseline.read_text())
    assert loaded["v2"]["hit_content_pct"] == 95.7


def test_main_regression_triggers_alarm(fake_eval_gate, tmp_path, monkeypatch):
    eval_set = tmp_path / "eval_set.json"
    eval_set.write_text("[]")
    baseline = tmp_path / "baseline.json"
    fake_eval_gate.write_baseline(_stub_report(95.0), baseline)

    monkeypatch.setattr(fake_eval_gate, "run_current_eval", lambda p: _stub_report(80.0))

    alerts: list[str] = []
    monkeypatch.setattr(fake_eval_gate, "alert_chris", lambda m: alerts.append(m))

    heal_called: list[dict] = []

    def fake_heal_dispatch(signal):
        heal_called.append({"signal_type": signal.signal_type, "value": signal.value})

    fake_self_heal = type(
        "M",
        (),
        {
            "HealingSignal": type("HS", (), {"__init__": lambda self, **kw: self.__dict__.update(kw)}),
            "dispatch": fake_heal_dispatch,
        },
    )
    monkeypatch.setitem(sys.modules, "self_heal", fake_self_heal)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "eval_gate",
            "--eval-set",
            str(eval_set),
            "--baseline",
            str(baseline),
            "--track",
            "stable",
        ],
    )
    rc = fake_eval_gate.main()
    assert rc == 1
    assert any("REGRESSION" in a for a in alerts)


def test_main_no_heal_flag_suppresses_dispatch(fake_eval_gate, tmp_path, monkeypatch):
    eval_set = tmp_path / "eval_set.json"
    eval_set.write_text("[]")
    baseline = tmp_path / "baseline.json"
    fake_eval_gate.write_baseline(_stub_report(95.0), baseline)

    monkeypatch.setattr(fake_eval_gate, "run_current_eval", lambda p: _stub_report(80.0))
    monkeypatch.setattr(fake_eval_gate, "alert_chris", lambda m: None)

    heal_called = []

    def fake_heal_dispatch(signal):
        heal_called.append(1)

    fake_self_heal = type(
        "M",
        (),
        {
            "HealingSignal": type("HS", (), {"__init__": lambda self, **kw: None}),
            "dispatch": fake_heal_dispatch,
        },
    )
    monkeypatch.setitem(sys.modules, "self_heal", fake_self_heal)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "eval_gate",
            "--eval-set",
            str(eval_set),
            "--baseline",
            str(baseline),
            "--track",
            "extended",
            "--no-heal",
        ],
    )
    rc = fake_eval_gate.main()
    assert rc == 1, "regression should still return 1"
    assert heal_called == [], "--no-heal should suppress dispatch"
