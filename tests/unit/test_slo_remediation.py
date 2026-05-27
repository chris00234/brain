from __future__ import annotations

import sys
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


def test_playbook_triggers_telegram_backlog_drain(monkeypatch, tmp_path):
    import slo_remediation

    monkeypatch.setenv("BRAIN_SLO_AUTOREMEDIATE", "on")
    monkeypatch.setattr(slo_remediation, "LOG_FILE", tmp_path / "slo_remediation.jsonl")
    monkeypatch.setattr(slo_remediation, "_should_fire", lambda _rule: True)
    monkeypatch.setattr(slo_remediation, "_record_fire", lambda _rule: None)
    fake_jr = type(sys)("job_registry")
    calls: list[str] = []

    def dispatch_job(name: str) -> int:
        calls.append(name)
        return 42

    fake_jr.dispatch_job = dispatch_job
    monkeypatch.setitem(sys.modules, "job_registry", fake_jr)

    out = slo_remediation.apply_direct_remediations([{"slo": "telegram_backlog_pending_count", "current": 1}])

    assert calls == ["llm_backlog_drain"]
    assert out["actions"][0]["action"] == "trigger:llm_backlog_drain"
    assert "telegram_backlog_pending_count" in (tmp_path / "slo_remediation.jsonl").read_text()


def test_playbook_logs_manual_for_telegram_health(monkeypatch, tmp_path):
    import slo_remediation

    monkeypatch.setenv("BRAIN_SLO_AUTOREMEDIATE", "on")
    monkeypatch.setattr(slo_remediation, "LOG_FILE", tmp_path / "slo_remediation.jsonl")
    monkeypatch.setattr(slo_remediation, "ESCALATION_LOG_FILE", tmp_path / "slo_escalations.jsonl")
    monkeypatch.setattr(slo_remediation, "_should_fire", lambda _rule: True)
    monkeypatch.setattr(slo_remediation, "_record_fire", lambda _rule: None)

    out = slo_remediation.apply_direct_remediations([{"slo": "telegram_direct_health", "current": 1}])

    assert out["actions"][0]["status"] == "manual_required"
    assert out["actions"][0]["escalated"] is True
    assert "manual_required" in (tmp_path / "slo_remediation.jsonl").read_text()
    escalation = (tmp_path / "slo_escalations.jsonl").read_text()
    assert '"route": "human"' in escalation
    assert "telegram_direct_health" in escalation


def test_playbook_qdrant_backup_job_name_exists(monkeypatch):
    import slo_remediation

    assert slo_remediation.PLAYBOOK["qdrant_backup_age_hours"].action == "qdrant_backup"


def test_playbook_new_2026_05_12_remediations_registered():
    """Verify the 2026-05-12 self-healing additions are wired correctly."""

    import slo_remediation

    expected = {
        "logs_dir_growth_24h_mb": "log_rotation",
        "atoms_confidence_stddev_1d": "confidence_calibration",
        "self_eval_drift_7d": "self_eval",
        "brain_server_rss_growth_1h_mb": "BRAIN_SCHED_MAX_HEAVY_JOBS",
    }
    for slo_name, action in expected.items():
        rule = slo_remediation.PLAYBOOK.get(slo_name)
        assert rule is not None, f"{slo_name} should have a remediation rule"
        assert rule.kind in {"trigger", "config"}
        assert rule.action == action


def test_playbook_triggers_calibration_refit_on_drift(monkeypatch, tmp_path):
    """2026-05-11: calibration drift breach must self-heal by triggering a
    fresh confidence_calibration fit, not just alert. The weekly cadence is
    too slow to let drift compound for 7 days before the next natural fit.
    """
    import slo_remediation

    monkeypatch.setenv("BRAIN_SLO_AUTOREMEDIATE", "on")
    monkeypatch.setattr(slo_remediation, "LOG_FILE", tmp_path / "slo_remediation.jsonl")
    monkeypatch.setattr(slo_remediation, "_should_fire", lambda _rule: True)
    monkeypatch.setattr(slo_remediation, "_record_fire", lambda _rule: None)
    fake_jr = type(sys)("job_registry")
    calls: list[str] = []

    def dispatch_job(name: str) -> int:
        calls.append(name)
        return 99999

    fake_jr.dispatch_job = dispatch_job
    monkeypatch.setitem(sys.modules, "job_registry", fake_jr)

    out = slo_remediation.apply_direct_remediations(
        [{"slo": "calibration_brier_drift_7d", "current": 0.08, "target": 0.05}]
    )

    assert calls == ["confidence_calibration"]
    assert out["actions"][0]["action"] == "trigger:confidence_calibration"


def test_playbook_starts_hermes_gateways(monkeypatch, tmp_path):
    import slo_remediation

    monkeypatch.setenv("BRAIN_SLO_AUTOREMEDIATE", "on")
    monkeypatch.setattr(slo_remediation, "LOG_FILE", tmp_path / "slo_remediation.jsonl")
    monkeypatch.setattr(slo_remediation, "_should_fire", lambda _rule: True)
    monkeypatch.setattr(slo_remediation, "_record_fire", lambda _rule: None)
    fake_jr = type(sys)("job_registry")
    calls: list[str] = []

    def dispatch_job(name: str) -> int:
        calls.append(name)
        return 18789

    fake_jr.dispatch_job = dispatch_job
    monkeypatch.setitem(sys.modules, "job_registry", fake_jr)

    out = slo_remediation.apply_direct_remediations([{"slo": "hermes_gateway_health", "current": 1}])

    assert calls == ["hermes_gateway_start"]
    assert out["actions"][0]["action"] == "trigger:hermes_gateway_start"
    assert "hermes_gateway_health" in (tmp_path / "slo_remediation.jsonl").read_text()


def test_playbook_escalates_stale_dispatch_attempts(monkeypatch, tmp_path):
    import slo_remediation

    monkeypatch.setenv("BRAIN_SLO_AUTOREMEDIATE", "on")
    monkeypatch.setattr(slo_remediation, "LOG_FILE", tmp_path / "slo_remediation.jsonl")
    monkeypatch.setattr(slo_remediation, "ESCALATION_LOG_FILE", tmp_path / "slo_escalations.jsonl")
    monkeypatch.setattr(slo_remediation, "_should_fire", lambda _rule: True)
    monkeypatch.setattr(slo_remediation, "_record_fire", lambda _rule: None)

    out = slo_remediation.apply_direct_remediations(
        [{"slo": "task_dispatch_stale_started_count", "current": 1}]
    )

    assert out["actions"][0]["status"] == "manual_required"
    assert out["actions"][0]["escalated"] is True
    assert "task_dispatch_stale_started_count" in (tmp_path / "slo_escalations.jsonl").read_text()


def test_playbook_escalates_autonomous_work_visibility_gap(monkeypatch, tmp_path):
    import slo_remediation

    monkeypatch.setenv("BRAIN_SLO_AUTOREMEDIATE", "on")
    monkeypatch.setattr(slo_remediation, "LOG_FILE", tmp_path / "slo_remediation.jsonl")
    monkeypatch.setattr(slo_remediation, "ESCALATION_LOG_FILE", tmp_path / "slo_escalations.jsonl")
    monkeypatch.setattr(slo_remediation, "_should_fire", lambda _rule: True)
    monkeypatch.setattr(slo_remediation, "_record_fire", lambda _rule: None)

    out = slo_remediation.apply_direct_remediations(
        [{"slo": "autonomous_work_visibility_gap_count", "current": 1}]
    )

    assert out["actions"][0]["status"] == "manual_required"
    assert "/brain/autonomous-work" in (tmp_path / "slo_remediation.jsonl").read_text()
    assert "autonomous_work_visibility_gap_count" in (tmp_path / "slo_escalations.jsonl").read_text()


def test_playbook_escalates_consecutive_rate_limited(monkeypatch, tmp_path):
    """Stuck remediation guard: after N rate_limited cycles in a row, emit a
    human-routed escalation so a wedged SLO (e.g. logs_dir_growth_24h_mb that
    fired 267× without resolving) cannot loop silently forever.
    """
    import slo_remediation

    monkeypatch.setenv("BRAIN_SLO_AUTOREMEDIATE", "on")
    monkeypatch.setattr(slo_remediation, "LOG_FILE", tmp_path / "slo_remediation.jsonl")
    monkeypatch.setattr(slo_remediation, "ESCALATION_LOG_FILE", tmp_path / "slo_escalations.jsonl")
    monkeypatch.setattr(slo_remediation, "_should_fire", lambda _rule: False)
    counter = {"v": 0}

    def fake_bump(_rule):
        counter["v"] += 1
        return counter["v"]

    monkeypatch.setattr(slo_remediation, "_bump_rate_limited", fake_bump)

    last_out = None
    for _ in range(slo_remediation.RATE_LIMITED_ESCALATION_THRESHOLD):
        last_out = slo_remediation.apply_direct_remediations(
            [{"slo": "logs_dir_growth_24h_mb", "current": 643.7}]
        )

    assert last_out is not None
    assert last_out["actions"][0]["status"] == "rate_limited"
    assert (
        last_out["actions"][0]["consecutive_rate_limited"]
        >= slo_remediation.RATE_LIMITED_ESCALATION_THRESHOLD
    )
    escalation_text = (tmp_path / "slo_escalations.jsonl").read_text()
    assert "stuck_rate_limited" in escalation_text
    assert "logs_dir_growth_24h_mb" in escalation_text


def test_stuck_rate_limited_escalation_is_idempotent(monkeypatch, tmp_path):
    """Once the streak crosses RATE_LIMITED_ESCALATION_THRESHOLD, follow-on
    cycles must not append new escalation rows. Otherwise a wedged SLO
    spams hundreds of identical escalations and buries new incidents.
    """
    import slo_remediation

    monkeypatch.setenv("BRAIN_SLO_AUTOREMEDIATE", "on")
    monkeypatch.setattr(slo_remediation, "LOG_FILE", tmp_path / "slo_remediation.jsonl")
    monkeypatch.setattr(slo_remediation, "ESCALATION_LOG_FILE", tmp_path / "slo_escalations.jsonl")
    monkeypatch.setattr(slo_remediation, "_should_fire", lambda _rule: False)
    counter = {"v": 0}

    def fake_bump(_rule):
        counter["v"] += 1
        return counter["v"]

    monkeypatch.setattr(slo_remediation, "_bump_rate_limited", fake_bump)

    # Fire enough cycles to cross the threshold AND keep going.
    cycles = slo_remediation.RATE_LIMITED_ESCALATION_THRESHOLD + 4
    for _ in range(cycles):
        slo_remediation.apply_direct_remediations([{"slo": "logs_dir_growth_24h_mb", "current": 643.7}])

    escalation_text = (tmp_path / "slo_escalations.jsonl").read_text().splitlines()
    stuck_lines = [line for line in escalation_text if "stuck_rate_limited" in line]
    assert (
        len(stuck_lines) == 1
    ), f"expected exactly one stuck_rate_limited escalation, saw {len(stuck_lines)}"


def test_playbook_resets_rate_limited_counter_on_success(monkeypatch, tmp_path):
    import slo_remediation

    monkeypatch.setenv("BRAIN_SLO_AUTOREMEDIATE", "on")
    monkeypatch.setattr(slo_remediation, "LOG_FILE", tmp_path / "slo_remediation.jsonl")

    set_keys: dict[str, str] = {}

    fake_store = type(sys)("brain_config_store")

    def store_set(key, value, **_kwargs):
        set_keys[key] = value

    def store_get(_key):
        return None

    fake_store.set = store_set
    fake_store.get = store_get
    monkeypatch.setitem(sys.modules, "brain_config_store", fake_store)

    rule = slo_remediation.PLAYBOOK["telegram_backlog_pending_count"]
    slo_remediation._record_fire(rule)
    counter_key = slo_remediation._consecutive_rate_limited_key(rule)
    assert set_keys.get(counter_key) == "0"
