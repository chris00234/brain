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


def _stub_report(content: float, source: float = 80.0, total: int = 100, loose: float | None = None) -> dict:
    return {
        "cases": total,
        "v2": {
            "total": total,
            "hit_content_pct": content,
            "hit_content_loose_pct": content if loose is None else loose,
            "hit_source_pct": source,
            "mean_rank": 1.5,
            "mean_latency_ms": 300,
        },
    }


def test_failure_breakdown_splits_retrieval_and_eval_stale_cases(fake_eval_gate):
    summary = fake_eval_gate._failure_breakdown(
        [
            {"query": "ok", "hit_content_loose": True, "hit_source": True},
            {"query": "retrieval miss", "hit_content_loose": False, "hit_source": False},
            {"query": "stale phrase", "hit_content_loose": False, "hit_source": True},
            {"query": "source alias", "hit_content_loose": True, "hit_source": False},
        ]
    )

    assert summary["content_failed"] == 2
    assert summary["source_failed"] == 2
    assert summary["both_failed"] == 1
    assert summary["content_only_failed"] == 1
    assert summary["source_only_failed"] == 1


def test_failure_analysis_classifies_fix_lanes(fake_eval_gate):
    analysis = fake_eval_gate._failure_analysis(
        [
            {
                "query": "pass",
                "hit_content_loose": True,
                "hit_source": True,
            },
            {
                "query": "stale phrase",
                "expected_source": "canonical/current.md",
                "expected_content": "old exact phrase",
                "hit_content_loose": False,
                "hit_source": True,
            },
            {
                "query": "source alias",
                "expected_source": "canonical/old.md",
                "expected_content": "right answer",
                "hit_content_loose": True,
                "hit_source": False,
            },
            {
                "query": "archived consolidation",
                "expected_source": "canonical/archived/chris/original-specific-memory.md",
                "expected_content": "specific wording",
                "hit_content_loose": False,
                "hit_source": False,
                "top_sources": ["canonical/chris/current-memory.md"],
            },
            {
                "query": "true miss",
                "expected_source": "canonical/project/alpha.md",
                "expected_content": "alpha",
                "hit_content_loose": False,
                "hit_source": False,
                "top_sources": ["canonical/project/beta.md"],
                "latency_ms": 1500,
            },
        ]
    )

    assert analysis["failed"] == 4
    assert analysis["buckets"]["stale_expected_content"]["count"] == 1
    assert analysis["buckets"]["source_alias_or_successor"]["count"] == 1
    assert analysis["buckets"]["canonical_consolidation_gap"]["count"] == 1
    assert analysis["buckets"]["retrieval_miss"]["count"] == 1
    assert analysis["secondary_flags"]["slow_failure"] == 1


def test_persist_default_track_uses_legacy_paths(fake_eval_gate, tmp_path):
    fake_eval_gate._persist_eval_report(_stub_report(95.7), track="default")
    assert (tmp_path / "logs" / "eval-report.json").exists()
    assert (tmp_path / "logs" / "eval-history.jsonl").exists()


def test_persist_named_track_uses_suffixed_paths(fake_eval_gate, tmp_path):
    fake_eval_gate._persist_eval_report(_stub_report(95.7), track="stable")
    assert (tmp_path / "logs" / "eval-report-stable.json").exists()
    assert (tmp_path / "logs" / "eval-history-stable.jsonl").exists()
    row = json.loads((tmp_path / "logs" / "eval-history-stable.jsonl").read_text().splitlines()[-1])
    assert row["hit_content_pct"] == 95.7
    assert row["hit_source_pct"] == 80.0
    # Default-track files should NOT exist
    assert not (tmp_path / "logs" / "eval-report.json").exists()


def test_persist_loose_metric_keeps_strict_and_selected_content(fake_eval_gate, tmp_path):
    report_in = _stub_report(70.0, loose=88.0)
    report_in["v2"]["per_test"] = [
        {
            "query": "q",
            "expected_source": "s",
            "expected_content": "c",
            "hit_content_loose": False,
            "hit_source": True,
            "top_sources": ["s"],
            "rank": 1,
        }
    ]
    fake_eval_gate._persist_eval_report(report_in, track="extended", content_metric="loose")

    report = json.loads((tmp_path / "logs" / "eval-report-extended.json").read_text())
    row = json.loads((tmp_path / "logs" / "eval-history-extended.jsonl").read_text().splitlines()[-1])

    assert report["accuracy"] == 88.0
    assert report["content_metric"] == "loose"
    assert row["hit_content_pct"] == 70.0
    assert row["hit_content_loose_pct"] == 88.0
    assert row["selected_content_pct"] == 88.0
    assert report["failure_breakdown"]["content_only_failed"] == 1
    assert report["failure_analysis"]["buckets"]["stale_expected_content"]["count"] == 1


def test_baseline_roundtrip(fake_eval_gate, tmp_path):
    baseline = tmp_path / "baseline.json"
    fake_eval_gate.write_baseline(_stub_report(95.7), baseline)
    loaded = fake_eval_gate.load_baseline(baseline)
    assert loaded is not None
    assert loaded["v2"]["hit_content_pct"] == 95.7
    assert "baseline_written_at" in loaded


def test_load_baseline_missing_returns_none(fake_eval_gate, tmp_path):
    assert fake_eval_gate.load_baseline(tmp_path / "no_such.json") is None


def test_alert_chris_uses_direct_telegram(fake_eval_gate, monkeypatch):
    calls = []
    monkeypatch.setitem(
        sys.modules,
        "telegram_alert",
        type(
            "_TelegramAlert",
            (),
            {
                "send_chris_telegram": staticmethod(
                    lambda body, source, severity: calls.append(
                        {"body": body, "source": source, "severity": severity}
                    )
                    or True
                )
            },
        ),
    )

    fake_eval_gate.alert_chris("regressed")

    assert calls == [
        {
            "body": "[BRAIN EVAL ALERT] regressed",
            "source": "eval_gate",
            "severity": "warn",
        }
    ]


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


def test_main_source_regression_triggers_alarm(fake_eval_gate, tmp_path, monkeypatch):
    eval_set = tmp_path / "eval_set.json"
    eval_set.write_text("[]")
    baseline = tmp_path / "baseline.json"
    fake_eval_gate.write_baseline(_stub_report(95.0, source=80.0), baseline)

    monkeypatch.setattr(fake_eval_gate, "run_current_eval", lambda p: _stub_report(95.0, source=50.0))

    alerts: list[str] = []
    monkeypatch.setattr(fake_eval_gate, "alert_chris", lambda m: alerts.append(m))

    heal_called: list[dict] = []

    def fake_heal_dispatch(signal):
        heal_called.append(
            {
                "metric": signal.metric,
                "value": signal.value,
                "baseline": signal.baseline,
                "context": signal.context,
            }
        )

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
            "extended",
        ],
    )
    rc = fake_eval_gate.main()
    assert rc == 1
    assert any("hit_source@5" in a for a in alerts)
    assert heal_called == [
        {
            "metric": "hit_source_pct",
            "value": 50.0,
            "baseline": 80.0,
            "context": {"delta": -30.0, "threshold": 10.0, "track": "extended"},
        }
    ]


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


def test_main_loose_metric_ignores_strict_only_drop(fake_eval_gate, tmp_path, monkeypatch):
    eval_set = tmp_path / "eval_set.json"
    eval_set.write_text("[]")
    baseline = tmp_path / "baseline.json"
    fake_eval_gate.write_baseline(_stub_report(95.0, loose=86.0), baseline)

    monkeypatch.setattr(fake_eval_gate, "run_current_eval", lambda p: _stub_report(70.0, loose=88.0))

    alerts: list[str] = []
    monkeypatch.setattr(fake_eval_gate, "alert_chris", lambda m: alerts.append(m))

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
            "--content-metric",
            "loose",
            "--no-heal",
        ],
    )
    rc = fake_eval_gate.main()
    assert rc == 0
    assert alerts == []
