from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "brain_core"))

import ops_readiness  # noqa: E402


def test_readiness_snapshot_surfaces_blocked_backup(tmp_path, monkeypatch):
    backup = tmp_path / "backup_restore_drill.json"
    backup.write_text(json.dumps({"all_ok": False}))
    monkeypatch.setattr(ops_readiness, "BACKUP_RESTORE_DRILL", backup)
    monkeypatch.setattr(ops_readiness, "RETRIEVAL_REGRESSION_LOG", tmp_path / "missing_retrieval.json")
    monkeypatch.setattr(ops_readiness, "RELEASE_READINESS_LOG", tmp_path / "missing_release.json")
    monkeypatch.setattr(ops_readiness, "SLO_REMEDIATION_LOG", tmp_path / "missing_slo.jsonl")

    out = ops_readiness.readiness_snapshot()

    assert out["status"] == "blocked"
    assert "backup_restore_drill" in out["blockers"]


def test_readiness_snapshot_blocks_missing_gate_artifacts(tmp_path, monkeypatch):
    backup = tmp_path / "backup_restore_drill.json"
    backup.write_text(json.dumps({"all_ok": True}))
    monkeypatch.setattr(ops_readiness, "BACKUP_RESTORE_DRILL", backup)
    monkeypatch.setattr(ops_readiness, "RETRIEVAL_REGRESSION_LOG", tmp_path / "missing_retrieval.json")
    monkeypatch.setattr(ops_readiness, "RELEASE_READINESS_LOG", tmp_path / "missing_release.json")
    monkeypatch.setattr(ops_readiness, "SLO_REMEDIATION_LOG", tmp_path / "missing_slo.jsonl")

    out = ops_readiness.readiness_snapshot()

    assert out["status"] == "blocked"
    assert "retrieval_regression" in out["blockers"]
    assert "release_readiness" in out["blockers"]


def test_remediation_incident_ledger_groups_statuses(tmp_path, monkeypatch):
    log = tmp_path / "slo_remediation.jsonl"
    log.write_text(
        "\n".join(
            [
                json.dumps({"slo": "logs_dir_total_mb", "status": "ok", "timestamp": "t1"}),
                json.dumps({"slo": "logs_dir_total_mb", "status": "rate_limited", "timestamp": "t2"}),
            ]
        )
    )
    monkeypatch.setattr(ops_readiness, "SLO_REMEDIATION_LOG", log)

    out = ops_readiness.remediation_incident_ledger()

    assert out["by_slo"]["logs_dir_total_mb"] == {"ok": 1, "rate_limited": 1}
    assert out["last_by_slo"]["logs_dir_total_mb"]["timestamp"] == "t2"


def test_slo_escalation_ledger_groups_routes(tmp_path, monkeypatch):
    log = tmp_path / "slo_escalations.jsonl"
    log.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "slo": "breaker_open_count",
                        "route": "human",
                        "escalation_status": "manual_required",
                    }
                ),
                json.dumps(
                    {
                        "slo": "backup_restore_drill_age_hours",
                        "route": "llm",
                        "escalation_status": "trigger_failed",
                    }
                ),
            ]
        )
    )
    monkeypatch.setattr(ops_readiness, "SLO_ESCALATION_LOG", log)

    out = ops_readiness.slo_escalation_ledger()

    assert out["by_route"] == {"human": 1, "llm": 1}
    assert out["by_slo"]["breaker_open_count"] == {"manual_required": 1}
