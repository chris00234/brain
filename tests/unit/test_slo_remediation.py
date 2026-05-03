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
