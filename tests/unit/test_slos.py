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
    # + 1 qdrant_backup_age_hours (2026-04-21 Qdrant migration)
    # + 1 neo4j_backup_age_hours (round-3 parity fix)
    # + 2 additional watchers (2026-04-23: boot_context_degraded_1h,
    #   self_eval_drift_7d)
    # + 1 brain_server_rss_mb (2026-04-26: FastAPI process memory watcher)
    # + 3 source-aware entry / alert reliability watchers
    # + 1 backup_restore_drill_age_hours (restore-readiness watcher)
    assert len(slos_module.SLOS) == 24
    assert "atoms_write_throughput_1h" in slos_module.SLOS
    assert "calibration_brier_drift_7d" in slos_module.SLOS
    assert "dispatch_failure_rate_1h" in slos_module.SLOS
    assert "agent_session_max_mb" in slos_module.SLOS
    assert "boot_context_degraded_1h" in slos_module.SLOS
    assert "self_eval_drift_7d" in slos_module.SLOS
    assert "logs_dir_total_mb" in slos_module.SLOS
    assert "qdrant_backup_age_hours" in slos_module.SLOS
    assert "neo4j_backup_age_hours" in slos_module.SLOS
    assert "backup_restore_drill_age_hours" in slos_module.SLOS
    assert "brain_server_rss_mb" in slos_module.SLOS
    assert "entry_contract_missing_pct" in slos_module.SLOS
    assert "telegram_backlog_pending_count" in slos_module.SLOS
    assert "telegram_direct_health" in slos_module.SLOS


def test_recall_v2_p95_lower_is_better(slos_module):
    slo = slos_module.SLOS["recall_v2_p95_ms"]
    # Loosened 2026-04-24 to 1000ms: quality-critical search + rerank should
    # not page the operator while still under the human-facing latency ceiling.
    assert slo.target == 1000.0
    assert slo.severity == "warning"
    # 1100ms > 1000 target -> breach (latency is lower-is-better)
    assert slos_module._is_breach(slo, 1100.0) is True
    assert slos_module._is_breach(slo, 900.0) is False


def test_content_hit_higher_is_better(slos_module):
    slo = slos_module.SLOS["recall_v2_content_hit_pct"]
    # Tightened 2026-04-21 Qdrant migration (was 95 on ChromaDB).
    assert slo.target == 96.0
    # 90% < 96% target → breach (recall is higher-is-better)
    assert slos_module._is_breach(slo, 90.0) is True
    assert slos_module._is_breach(slo, 96.5) is False


def test_breaker_open_count_zero_target(slos_module):
    slo = slos_module.SLOS["breaker_open_count"]
    assert slo.target == 0.0
    assert slos_module._is_breach(slo, 1.0) is True
    assert slos_module._is_breach(slo, 0.0) is False


def test_entry_contract_missing_pct_zero_target(slos_module):
    slo = slos_module.SLOS["entry_contract_missing_pct"]
    assert slo.target == 0.0
    assert slo.severity == "critical"
    assert slos_module._is_breach(slo, 0.1) is True
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
    assert result.delta == 150.0 - 1000.0


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


def test_failed_alert_does_not_persist_rate_limit(slos_module, monkeypatch):
    sent: list[str] = []
    fake_store: dict[tuple[str, str], float] = {}
    outcomes = iter([False, True])

    def fake_alert(slo, actual):
        sent.append(slo.name)
        return next(outcomes)

    monkeypatch.setattr(slos_module, "_alert_telegram", fake_alert)
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

    assert slos_module.maybe_alert(result) is False
    assert fake_store == {}
    assert slos_module.maybe_alert(result) is True
    assert fake_store[(slo.name, slo.severity)] > 0
    assert sent == [slo.name, slo.name]


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


def test_brain_server_rss_ignores_checker_commands(slos_module, monkeypatch, tmp_path):
    """The RSS SLO must not accidentally measure the short-lived SLO runner.

    `pgrep -f brain/server.py` matched checker command text and produced 0 MB
    or the wrong pid. The process-table parser should require the exact
    server.py path as a Python argv entry and choose the real server RSS.
    """

    brain_dir = tmp_path / "brain"
    brain_dir.mkdir()
    server_py = brain_dir / "server.py"
    server_py.write_text("# fake server\n")
    monkeypatch.setattr(slos_module, "BRAIN_DIR", brain_dir)
    monkeypatch.setattr(slos_module.os, "getpid", lambda: 123)

    class Completed:
        def __init__(self, stdout: str):
            self.stdout = stdout

    def fake_run(cmd, **_kwargs):
        assert cmd[:3] == ["ps", "-axo", "pid=,rss=,command="]
        return Completed(
            "\n".join(
                [
                    # SLO checker / shell command mentions server.py but is not the server.
                    f" 123 100000 /bin/zsh -lc pgrep -f {server_py}",
                    f" 456 200000 /opt/homebrew/bin/python -c import sys; print('{server_py}')",
                    # Real FastAPI server process.
                    f" 789 3145728 /opt/homebrew/bin/python {server_py}",
                ]
            )
        )

    monkeypatch.setattr(slos_module.subprocess, "run", fake_run)
    assert slos_module._brain_server_rss_kb_from_process_table() == 3145728


def test_brain_server_rss_uses_current_process_when_in_server(slos_module, monkeypatch, tmp_path):
    brain_dir = tmp_path / "brain"
    brain_dir.mkdir()
    server_py = brain_dir / "server.py"
    server_py.write_text("# fake server\n")
    monkeypatch.setattr(slos_module, "BRAIN_DIR", brain_dir)
    monkeypatch.setattr(slos_module.sys, "argv", [str(server_py)])
    monkeypatch.setattr(slos_module.os, "getpid", lambda: 789)
    monkeypatch.setattr(slos_module, "_rss_kb_for_pid", lambda pid: 2048 if pid == 789 else 0)
    monkeypatch.setattr(slos_module, "_brain_server_rss_kb_from_process_table", lambda: 999999)

    assert slos_module._measure_brain_server_rss_mb() == 2.0


def test_run_invokes_direct_remediation_for_breaches(slos_module, monkeypatch):
    slo = slos_module.SLOS["outbox_pending_count"]
    monkeypatch.setattr(
        slos_module,
        "check_all",
        lambda: [slos_module.SLOResult(slo=slo, actual=50.0, breached=True, delta=30.0)],
    )
    monkeypatch.setattr(slos_module, "maybe_alert", lambda _result: False)
    calls: list[list[dict]] = []
    fake_remediation = type(sys)("slo_remediation")
    fake_remediation.apply_direct_remediations = lambda violations: calls.append(violations) or {
        "actions": []
    }
    monkeypatch.setitem(sys.modules, "slo_remediation", fake_remediation)

    out = slos_module.run()

    assert out["breached"] == 1
    assert calls == [
        [{"slo": "outbox_pending_count", "current": 50.0, "target": 20.0, "severity": "warning"}]
    ]
    assert out["remediation"] == {"actions": []}
