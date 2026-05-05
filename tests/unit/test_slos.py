"""Unit tests for brain_core.slos (Phase E1)."""

from __future__ import annotations

import json
import sqlite3
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
    # + 1 openclaw_gateway_health (agent-dispatch gateway watcher)
    # + 1 task_dispatch_stale_started_count (unclosed dispatch attempt watcher)
    # + 1 task_failure_lesson_missing_count (Reflexion lesson coverage watcher)
    assert len(slos_module.SLOS) == 27
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
    assert "openclaw_gateway_health" in slos_module.SLOS
    assert "task_dispatch_stale_started_count" in slos_module.SLOS
    assert "task_failure_lesson_missing_count" in slos_module.SLOS


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


def test_openclaw_gateway_health_zero_target(slos_module):
    slo = slos_module.SLOS["openclaw_gateway_health"]
    assert slo.target == 0.0
    assert slo.severity == "critical"
    assert slos_module._is_breach(slo, 1.0) is True
    assert slos_module._is_breach(slo, 0.0) is False


def test_measure_openclaw_gateway_health_success(slos_module, monkeypatch):
    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    calls: list[tuple[tuple[str, int], float]] = []

    def fake_create_connection(addr, timeout):
        calls.append((addr, timeout))
        return FakeSocket()

    monkeypatch.setattr(slos_module.socket, "create_connection", fake_create_connection)

    assert slos_module._measure_openclaw_gateway_health() == 0.0
    assert calls == [(("127.0.0.1", 18789), 1.0)]


def test_measure_openclaw_gateway_health_failure(slos_module, monkeypatch):
    def fake_create_connection(_addr, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(slos_module.socket, "create_connection", fake_create_connection)

    assert slos_module._measure_openclaw_gateway_health() == 1.0


def test_measure_task_dispatch_stale_started_count(slos_module, monkeypatch, tmp_path):
    db = tmp_path / "autonomy.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """CREATE TABLE task_dispatch_attempts (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            "INSERT INTO task_dispatch_attempts (id, status, started_at) VALUES (?, ?, ?)",
            ("old_started", "started", "2000-01-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO task_dispatch_attempts (id, status, started_at) VALUES (?, ?, ?)",
            ("old_closed", "completed", "2000-01-01T00:00:00+00:00"),
        )
    monkeypatch.setattr(slos_module, "AUTONOMY_DB", db)

    assert slos_module._measure_task_dispatch_stale_started_count() == 1.0


def test_measure_task_failure_lesson_missing_count(slos_module, monkeypatch, tmp_path):
    db = tmp_path / "autonomy.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """CREATE TABLE task_dispatch_attempts (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                completed_at TEXT,
                metadata TEXT DEFAULT '{}'
            )"""
        )
        conn.execute(
            "INSERT INTO task_dispatch_attempts (id, status, completed_at, metadata) VALUES (?, ?, ?, ?)",
            ("missing", "failed", "2000-01-01T00:00:00+00:00", "{}"),
        )
        conn.execute(
            "INSERT INTO task_dispatch_attempts (id, status, completed_at, metadata) VALUES (?, ?, ?, ?)",
            (
                "failed_record",
                "deferred",
                "2000-01-01T00:00:00+00:00",
                '{"failure_lesson_status":"record_failed"}',
            ),
        )
        conn.execute(
            "INSERT INTO task_dispatch_attempts (id, status, completed_at, metadata) VALUES (?, ?, ?, ?)",
            ("recorded", "failed", "2000-01-01T00:00:00+00:00", '{"failure_lesson_status":"recorded"}'),
        )
        conn.execute(
            "INSERT INTO task_dispatch_attempts (id, status, completed_at, metadata) VALUES (?, ?, ?, ?)",
            ("completed", "completed", "2000-01-01T00:00:00+00:00", "{}"),
        )
    monkeypatch.setattr(slos_module, "AUTONOMY_DB", db)

    assert slos_module._measure_task_failure_lesson_missing_count() == 2.0


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


def test_measure_recall_v2_p95_ignores_eval_traffic_class(slos_module, monkeypatch, tmp_path):
    db = tmp_path / "metrics_history.db"
    monkeypatch.setattr(slos_module, "METRICS_DB", db)
    db.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "routes": {
            "/recall/v2": {"window_count": 30, "p95_ms": 120.0},
            "/recall/v2#eval": {"window_count": 30, "p95_ms": 2400.0},
        }
    }
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE metrics_snapshots (id INTEGER PRIMARY KEY, timestamp TEXT, payload TEXT)")
        conn.execute(
            "INSERT INTO metrics_snapshots (timestamp, payload) VALUES ('now', ?)", (json.dumps(payload),)
        )

    assert slos_module._measure_recall_v2_p95() == 120.0


def test_measure_recall_v2_p95_prefers_live_prod_samples(slos_module, monkeypatch, tmp_path):
    db = tmp_path / "metrics_history.db"
    monkeypatch.setattr(slos_module, "METRICS_DB", db)
    db.parent.mkdir(parents=True, exist_ok=True)
    stale_payload = {"routes": {"/recall/v2": {"window_count": 30, "p95_ms": 2400.0}}}
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE metrics_snapshots (id INTEGER PRIMARY KEY, timestamp TEXT, payload TEXT)")
        conn.execute(
            "INSERT INTO metrics_snapshots (timestamp, payload) VALUES ('stale', ?)",
            (json.dumps(stale_payload),),
        )

    fake_metrics = type(sys)("metrics_buffer")

    class FakeBuffer:
        @staticmethod
        def snapshot():
            return {"routes": {"/recall/v2": {"window_count": 30, "p95_ms": 180.0}}}

    fake_metrics.metrics_buffer = FakeBuffer()
    monkeypatch.setitem(sys.modules, "metrics_buffer", fake_metrics)

    assert slos_module._measure_recall_v2_p95() == 180.0


def test_measure_recall_v2_p95_live_warmup_suppresses_stale_snapshot(slos_module, monkeypatch, tmp_path):
    db = tmp_path / "metrics_history.db"
    monkeypatch.setattr(slos_module, "METRICS_DB", db)
    db.parent.mkdir(parents=True, exist_ok=True)
    stale_payload = {"routes": {"/recall/v2": {"window_count": 30, "p95_ms": 2400.0}}}
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE metrics_snapshots (id INTEGER PRIMARY KEY, timestamp TEXT, payload TEXT)")
        conn.execute(
            "INSERT INTO metrics_snapshots (timestamp, payload) VALUES ('stale', ?)",
            (json.dumps(stale_payload),),
        )

    fake_metrics = type(sys)("metrics_buffer")

    class FakeBuffer:
        @staticmethod
        def snapshot():
            return {"routes": {"/recall/v2": {"window_count": 5, "p95_ms": 2000.0}}}

    fake_metrics.metrics_buffer = FakeBuffer()
    monkeypatch.setitem(sys.modules, "metrics_buffer", fake_metrics)

    assert slos_module._measure_recall_v2_p95() == 0.0
