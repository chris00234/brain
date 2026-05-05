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
    monkeypatch.setattr(ops_readiness, "ADVERSARIAL_EVAL_REPORT", tmp_path / "missing_adversarial.json")
    monkeypatch.setattr(ops_readiness, "RAGAS_EVAL_REPORT", tmp_path / "missing_ragas.json")
    monkeypatch.setattr(ops_readiness, "RELEASE_READINESS_LOG", tmp_path / "missing_release.json")
    monkeypatch.setattr(ops_readiness, "SLO_REMEDIATION_LOG", tmp_path / "missing_slo.jsonl")
    monkeypatch.setattr(ops_readiness, "openclaw_gateway_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "ragas_eval_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "crag_regression_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "crag_correction_regression_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "holdout_eval_snapshot", lambda: {"status": "ok"})

    out = ops_readiness.readiness_snapshot()

    assert out["status"] == "blocked"
    assert "backup_restore_drill" in out["blockers"]


def test_readiness_snapshot_blocks_missing_gate_artifacts(tmp_path, monkeypatch):
    backup = tmp_path / "backup_restore_drill.json"
    backup.write_text(json.dumps({"all_ok": True}))
    monkeypatch.setattr(ops_readiness, "BACKUP_RESTORE_DRILL", backup)
    monkeypatch.setattr(ops_readiness, "RETRIEVAL_REGRESSION_LOG", tmp_path / "missing_retrieval.json")
    monkeypatch.setattr(ops_readiness, "ADVERSARIAL_EVAL_REPORT", tmp_path / "missing_adversarial.json")
    monkeypatch.setattr(ops_readiness, "HOLDOUT_EVAL_REPORT", tmp_path / "missing_holdout.json")
    monkeypatch.setattr(ops_readiness, "RAGAS_EVAL_REPORT", tmp_path / "missing_ragas.json")
    monkeypatch.setattr(ops_readiness, "RELEASE_READINESS_LOG", tmp_path / "missing_release.json")
    monkeypatch.setattr(ops_readiness, "SLO_REMEDIATION_LOG", tmp_path / "missing_slo.jsonl")
    monkeypatch.setattr(ops_readiness, "openclaw_gateway_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "crag_regression_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "crag_correction_regression_snapshot", lambda: {"status": "ok"})

    out = ops_readiness.readiness_snapshot()

    assert out["status"] == "blocked"
    assert "retrieval_regression" in out["blockers"]
    assert "adversarial_eval" in out["blockers"]
    assert "holdout_eval" in out["blockers"]
    assert "ragas_eval" in out["blockers"]
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


def test_readiness_snapshot_blocks_openclaw_gateway(tmp_path, monkeypatch):
    backup = tmp_path / "backup_restore_drill.json"
    retrieval = tmp_path / "retrieval_regression.json"
    release = tmp_path / "release_readiness.json"
    backup.write_text(json.dumps({"all_ok": True}))
    retrieval.write_text(json.dumps({"status": "ok"}))
    release.write_text(json.dumps({"status": "ok"}))
    monkeypatch.setattr(ops_readiness, "BACKUP_RESTORE_DRILL", backup)
    monkeypatch.setattr(ops_readiness, "RETRIEVAL_REGRESSION_LOG", retrieval)
    monkeypatch.setattr(ops_readiness, "adversarial_eval_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "RELEASE_READINESS_LOG", release)
    monkeypatch.setattr(ops_readiness, "SLO_REMEDIATION_LOG", tmp_path / "missing_slo.jsonl")
    monkeypatch.setattr(ops_readiness, "SLO_ESCALATION_LOG", tmp_path / "missing_slo_escalations.jsonl")
    monkeypatch.setattr(ops_readiness, "openclaw_gateway_snapshot", lambda: {"status": "blocked"})
    monkeypatch.setattr(ops_readiness, "ragas_eval_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "crag_regression_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "crag_correction_regression_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "holdout_eval_snapshot", lambda: {"status": "ok"})

    out = ops_readiness.readiness_snapshot()

    assert out["status"] == "blocked"
    assert "openclaw_gateway" in out["blockers"]
    assert out["openclaw_gateway"]["status"] == "blocked"


def test_ui_parity_audit_snapshot_blocks_missing_coverage(tmp_path, monkeypatch):
    report = tmp_path / "ui_parity_audit.json"
    report.write_text(json.dumps({"status": "ok", "required": 10, "ok": 9, "blocked": 1}))
    monkeypatch.setattr(ops_readiness, "UI_PARITY_AUDIT_LOG", report)

    out = ops_readiness.ui_parity_audit_snapshot()

    assert out["status"] == "blocked"
    assert out["min_required"] == 10


def test_readiness_snapshot_blocks_ui_parity_audit(tmp_path, monkeypatch):
    backup = tmp_path / "backup_restore_drill.json"
    retrieval = tmp_path / "retrieval_regression.json"
    release = tmp_path / "release_readiness.json"
    ui_parity = tmp_path / "ui_parity_audit.json"
    backup.write_text(json.dumps({"all_ok": True}))
    retrieval.write_text(json.dumps({"status": "ok"}))
    release.write_text(json.dumps({"status": "ok"}))
    ui_parity.write_text(json.dumps({"status": "blocked", "required": 9, "ok": 8, "blocked": 1}))
    monkeypatch.setattr(ops_readiness, "BACKUP_RESTORE_DRILL", backup)
    monkeypatch.setattr(ops_readiness, "RETRIEVAL_REGRESSION_LOG", retrieval)
    monkeypatch.setattr(ops_readiness, "RELEASE_READINESS_LOG", release)
    monkeypatch.setattr(ops_readiness, "UI_PARITY_AUDIT_LOG", ui_parity)
    monkeypatch.setattr(ops_readiness, "SLO_REMEDIATION_LOG", tmp_path / "missing_slo.jsonl")
    monkeypatch.setattr(ops_readiness, "SLO_ESCALATION_LOG", tmp_path / "missing_slo_escalations.jsonl")
    monkeypatch.setattr(ops_readiness, "adversarial_eval_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "ragas_eval_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "crag_regression_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "crag_correction_regression_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "holdout_eval_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "openclaw_gateway_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "source_governance_readiness_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "skill_promotion_readiness_snapshot", lambda: {"status": "ok"})

    out = ops_readiness.readiness_snapshot()

    assert out["status"] == "blocked"
    assert "ui_parity_audit" in out["blockers"]
    assert out["ui_parity_audit"]["status"] == "blocked"


def test_ragas_eval_snapshot_blocks_missing_metrics(tmp_path, monkeypatch):
    report = tmp_path / "eval-report-ragas.json"
    report.write_text(json.dumps({"status": "ok", "ragas": {"answer_relevance_mean": 0.9}}))
    monkeypatch.setattr(ops_readiness, "RAGAS_EVAL_REPORT", report)

    out = ops_readiness.ragas_eval_snapshot()

    assert out["status"] == "missing_metrics"


def test_adversarial_eval_snapshot_blocks_low_accuracy(tmp_path, monkeypatch):
    report = tmp_path / "eval-report-adversarial.json"
    report.write_text(json.dumps({"accuracy": 79.0, "source_accuracy": 100.0}))
    monkeypatch.setattr(ops_readiness, "ADVERSARIAL_EVAL_REPORT", report)

    out = ops_readiness.adversarial_eval_snapshot()

    assert out["status"] == "breached"
    assert out["min_accuracy"] == 80.0


def test_crag_regression_snapshot_blocks_false_accepts(tmp_path, monkeypatch):
    report = tmp_path / "crag_regression.json"
    report.write_text(json.dumps({"status": "ok", "safety_rate": 97.5, "dangerous_false_accepts": 1}))
    monkeypatch.setattr(ops_readiness, "CRAG_REGRESSION_LOG", report)

    out = ops_readiness.crag_regression_snapshot()

    assert out["status"] == "breached"
    assert out["dangerous_false_accepts"] == 1


def test_ragas_eval_snapshot_ok_when_thresholds_pass(tmp_path, monkeypatch):
    report = tmp_path / "eval-report-ragas.json"
    report.write_text(
        json.dumps(
            {
                "ragas": {
                    "faithfulness_mean": 0.8,
                    "answer_relevance_mean": 0.75,
                }
            }
        )
    )
    monkeypatch.setattr(ops_readiness, "RAGAS_EVAL_REPORT", report)

    out = ops_readiness.ragas_eval_snapshot()

    assert out["status"] == "ok"
    assert out["faithfulness_mean"] == 0.8


def test_ragas_eval_snapshot_keeps_low_relevance_informational(tmp_path, monkeypatch):
    report = tmp_path / "eval-report-ragas.json"
    report.write_text(
        json.dumps(
            {
                "ragas": {
                    "faithfulness_mean": 1.0,
                    "answer_relevance_mean": 0.5,
                }
            }
        )
    )
    monkeypatch.setattr(ops_readiness, "RAGAS_EVAL_REPORT", report)

    out = ops_readiness.ragas_eval_snapshot()

    assert out["status"] == "ok"
    assert out["answer_relevance_status"] == "low_info"
    assert out["generated_answer_gate"] is False


def test_ragas_eval_snapshot_blocks_low_generated_answer_relevance(tmp_path, monkeypatch):
    report = tmp_path / "eval-report-ragas.json"
    report.write_text(
        json.dumps(
            {
                "ragas": {
                    "faithfulness_mean": 1.0,
                    "answer_relevance_mean": 0.5,
                    "answer_source": "generated",
                    "answer_source_counts": {"generated": 8},
                    "n": 8,
                }
            }
        )
    )
    monkeypatch.setattr(ops_readiness, "RAGAS_EVAL_REPORT", report)

    out = ops_readiness.ragas_eval_snapshot()

    assert out["status"] == "breached"
    assert out["answer_relevance_status"] == "low"
    assert out["generated_answer_gate"] is True


def test_ragas_eval_snapshot_blocks_small_generated_answer_seed(tmp_path, monkeypatch):
    report = tmp_path / "eval-report-ragas.json"
    report.write_text(
        json.dumps(
            {
                "ragas": {
                    "faithfulness_mean": 1.0,
                    "answer_relevance_mean": 0.8,
                    "answer_source": "generated",
                    "answer_source_counts": {"generated": 3},
                    "n": 3,
                }
            }
        )
    )
    monkeypatch.setattr(ops_readiness, "RAGAS_EVAL_REPORT", report)

    out = ops_readiness.ragas_eval_snapshot()

    assert out["status"] == "breached"
    assert out["case_count"] == 3
    assert out["generated_min_cases"] == 8


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


def test_readiness_snapshot_blocks_source_governance(tmp_path, monkeypatch):
    backup = tmp_path / "backup_restore_drill.json"
    retrieval = tmp_path / "retrieval_regression.json"
    release = tmp_path / "release_readiness.json"
    backup.write_text(json.dumps({"all_ok": True}))
    retrieval.write_text(json.dumps({"status": "ok"}))
    release.write_text(json.dumps({"status": "ok"}))
    monkeypatch.setattr(ops_readiness, "BACKUP_RESTORE_DRILL", backup)
    monkeypatch.setattr(ops_readiness, "RETRIEVAL_REGRESSION_LOG", retrieval)
    monkeypatch.setattr(ops_readiness, "adversarial_eval_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "ragas_eval_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "RELEASE_READINESS_LOG", release)
    monkeypatch.setattr(ops_readiness, "SLO_REMEDIATION_LOG", tmp_path / "missing_slo.jsonl")
    monkeypatch.setattr(ops_readiness, "SLO_ESCALATION_LOG", tmp_path / "missing_slo_escalations.jsonl")
    monkeypatch.setattr(ops_readiness, "openclaw_gateway_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "crag_regression_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "crag_correction_regression_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "holdout_eval_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(
        ops_readiness,
        "source_governance_readiness_snapshot",
        lambda: {"status": "blocked", "blockers": ["personal"]},
    )

    out = ops_readiness.readiness_snapshot()

    assert out["status"] == "blocked"
    assert "source_governance" in out["blockers"]
    assert out["source_governance"]["blockers"] == ["personal"]


def test_adversarial_eval_snapshot_blocks_forbidden_negative_hits(tmp_path, monkeypatch):
    report = tmp_path / "eval-report-adversarial.json"
    report.write_text(
        json.dumps(
            {
                "accuracy": 100.0,
                "source_accuracy": 100.0,
                "v2": {"negative_pass_pct": 90.0, "forbidden_hit_count": 1},
            }
        )
    )
    monkeypatch.setattr(ops_readiness, "ADVERSARIAL_EVAL_REPORT", report)

    out = ops_readiness.adversarial_eval_snapshot()

    assert out["status"] == "breached"
    assert out["negative_pass_pct"] == 90.0
    assert out["forbidden_hit_count"] == 1


def test_holdout_eval_snapshot_blocks_small_or_low_accuracy(tmp_path, monkeypatch):
    report = tmp_path / "eval-report-holdout.json"
    report.write_text(
        json.dumps(
            {
                "accuracy": 100.0,
                "source_accuracy": 100.0,
                "v2": {"total": 3, "negative_pass_pct": 100.0, "forbidden_hit_count": 0},
            }
        )
    )
    monkeypatch.setattr(ops_readiness, "HOLDOUT_EVAL_REPORT", report)

    out = ops_readiness.holdout_eval_snapshot()

    assert out["status"] == "breached"
    assert out["min_cases"] == 10


def test_holdout_eval_snapshot_ok_when_thresholds_pass(tmp_path, monkeypatch):
    report = tmp_path / "eval-report-holdout.json"
    report.write_text(
        json.dumps(
            {
                "accuracy": 95.0,
                "source_accuracy": 100.0,
                "v2": {"total": 10, "negative_pass_pct": 100.0, "forbidden_hit_count": 0},
            }
        )
    )
    monkeypatch.setattr(ops_readiness, "HOLDOUT_EVAL_REPORT", report)

    out = ops_readiness.holdout_eval_snapshot()

    assert out["status"] == "ok"
    assert out["total"] == 10


def test_readiness_snapshot_blocks_crag_regression(tmp_path, monkeypatch):
    backup = tmp_path / "backup_restore_drill.json"
    retrieval = tmp_path / "retrieval_regression.json"
    release = tmp_path / "release_readiness.json"
    backup.write_text(json.dumps({"all_ok": True}))
    retrieval.write_text(json.dumps({"status": "ok"}))
    release.write_text(json.dumps({"status": "ok"}))
    monkeypatch.setattr(ops_readiness, "BACKUP_RESTORE_DRILL", backup)
    monkeypatch.setattr(ops_readiness, "RETRIEVAL_REGRESSION_LOG", retrieval)
    monkeypatch.setattr(ops_readiness, "adversarial_eval_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "ragas_eval_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(
        ops_readiness,
        "crag_regression_snapshot",
        lambda: {"status": "breached", "dangerous_false_accepts": 1},
    )
    monkeypatch.setattr(ops_readiness, "crag_correction_regression_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "holdout_eval_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "RELEASE_READINESS_LOG", release)
    monkeypatch.setattr(ops_readiness, "SLO_REMEDIATION_LOG", tmp_path / "missing_slo.jsonl")
    monkeypatch.setattr(ops_readiness, "SLO_ESCALATION_LOG", tmp_path / "missing_slo_escalations.jsonl")
    monkeypatch.setattr(ops_readiness, "openclaw_gateway_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "source_governance_readiness_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "skill_promotion_readiness_snapshot", lambda: {"status": "ok"})

    out = ops_readiness.readiness_snapshot()

    assert out["status"] == "blocked"
    assert "crag_regression" in out["blockers"]


def test_readiness_snapshot_blocks_skill_promotion(tmp_path, monkeypatch):
    backup = tmp_path / "backup_restore_drill.json"
    retrieval = tmp_path / "retrieval_regression.json"
    release = tmp_path / "release_readiness.json"
    backup.write_text(json.dumps({"all_ok": True}))
    retrieval.write_text(json.dumps({"status": "ok"}))
    release.write_text(json.dumps({"status": "ok"}))
    monkeypatch.setattr(ops_readiness, "BACKUP_RESTORE_DRILL", backup)
    monkeypatch.setattr(ops_readiness, "RETRIEVAL_REGRESSION_LOG", retrieval)
    monkeypatch.setattr(ops_readiness, "adversarial_eval_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "ragas_eval_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "RELEASE_READINESS_LOG", release)
    monkeypatch.setattr(ops_readiness, "SLO_REMEDIATION_LOG", tmp_path / "missing_slo.jsonl")
    monkeypatch.setattr(ops_readiness, "SLO_ESCALATION_LOG", tmp_path / "missing_slo_escalations.jsonl")
    monkeypatch.setattr(ops_readiness, "openclaw_gateway_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "crag_regression_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "crag_correction_regression_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "holdout_eval_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "source_governance_readiness_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(
        ops_readiness,
        "skill_promotion_readiness_snapshot",
        lambda: {"status": "blocked", "blockers": ["auto-x"]},
    )

    out = ops_readiness.readiness_snapshot()

    assert out["status"] == "blocked"
    assert "skill_promotion" in out["blockers"]
    assert out["skill_promotion"]["blockers"] == ["auto-x"]


def test_crag_correction_regression_snapshot_blocks_failed_recovery(tmp_path, monkeypatch):
    report = tmp_path / "crag_correction_regression.json"
    report.write_text(
        json.dumps(
            {
                "status": "ok",
                "recovery_needed": 4,
                "recovered": 3,
                "failed_recoveries": 1,
                "recovery_rate": 75.0,
            }
        )
    )
    monkeypatch.setattr(ops_readiness, "CRAG_CORRECTION_REGRESSION_LOG", report)

    out = ops_readiness.crag_correction_regression_snapshot()

    assert out["status"] == "breached"
    assert out["min_recovery_rate"] == 80.0


def test_crag_correction_regression_snapshot_blocks_insufficient_coverage(tmp_path, monkeypatch):
    report = tmp_path / "crag_correction_regression.json"
    report.write_text(
        json.dumps(
            {
                "status": "ok",
                "recovery_needed": 1,
                "recovered": 1,
                "failed_recoveries": 0,
                "recovery_rate": 100.0,
            }
        )
    )
    monkeypatch.setattr(ops_readiness, "CRAG_CORRECTION_REGRESSION_LOG", report)

    out = ops_readiness.crag_correction_regression_snapshot()

    assert out["status"] == "insufficient_coverage"
    assert out["min_recovery_cases"] == 3


def test_readiness_snapshot_blocks_crag_correction_regression(tmp_path, monkeypatch):
    backup = tmp_path / "backup_restore_drill.json"
    retrieval = tmp_path / "retrieval_regression.json"
    release = tmp_path / "release_readiness.json"
    backup.write_text(json.dumps({"all_ok": True}))
    retrieval.write_text(json.dumps({"status": "ok"}))
    release.write_text(json.dumps({"status": "ok"}))
    monkeypatch.setattr(ops_readiness, "BACKUP_RESTORE_DRILL", backup)
    monkeypatch.setattr(ops_readiness, "RETRIEVAL_REGRESSION_LOG", retrieval)
    monkeypatch.setattr(ops_readiness, "adversarial_eval_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "ragas_eval_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "crag_regression_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(
        ops_readiness,
        "crag_correction_regression_snapshot",
        lambda: {"status": "breached", "failed_recoveries": 1},
    )
    monkeypatch.setattr(ops_readiness, "holdout_eval_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "RELEASE_READINESS_LOG", release)
    monkeypatch.setattr(ops_readiness, "SLO_REMEDIATION_LOG", tmp_path / "missing_slo.jsonl")
    monkeypatch.setattr(ops_readiness, "SLO_ESCALATION_LOG", tmp_path / "missing_slo_escalations.jsonl")
    monkeypatch.setattr(ops_readiness, "openclaw_gateway_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "source_governance_readiness_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "skill_promotion_readiness_snapshot", lambda: {"status": "ok"})

    out = ops_readiness.readiness_snapshot()

    assert out["status"] == "blocked"
    assert "crag_correction_regression" in out["blockers"]


def test_crag_llm_correction_regression_snapshot_is_nonblocking(tmp_path, monkeypatch):
    report = tmp_path / "crag_llm_correction_regression.json"
    report.write_text(
        json.dumps(
            {
                "status": "ok",
                "recovery_needed": 2,
                "recovered": 1,
                "failed_recoveries": 1,
                "recovery_rate": 50.0,
            }
        )
    )
    monkeypatch.setattr(ops_readiness, "CRAG_LLM_CORRECTION_REGRESSION_LOG", report)

    out = ops_readiness.crag_llm_correction_regression_snapshot()

    assert out["status"] == "breached"
    assert out["readiness_blocking"] is False
    assert out["coverage_level"] == "exploratory"


def test_ragas_eval_snapshot_reports_pending_coverage_when_eval_set_grew(tmp_path, monkeypatch):
    report = tmp_path / "eval-report-ragas.json"
    report.write_text(
        json.dumps(
            {
                "status": "ok",
                "cases": 8,
                "ragas": {
                    "n": 8,
                    "faithfulness_mean": 0.9,
                    "answer_relevance_mean": 0.8,
                    "answer_source": "generated",
                    "answer_source_counts": {"generated": 8},
                },
            }
        )
    )
    eval_set = tmp_path / "eval_set_ragas_answers.json"
    eval_set.write_text(json.dumps([{"query": str(i)} for i in range(12)]))
    monkeypatch.setattr(ops_readiness, "RAGAS_EVAL_REPORT", report)
    monkeypatch.setattr(ops_readiness, "RAGAS_ANSWER_EVAL_SET", eval_set)

    out = ops_readiness.ragas_eval_snapshot()

    assert out["status"] == "breached"
    assert out["case_count"] == 8
    assert out["eval_set_case_count"] == 12
    assert out["generated_target_cases"] == 12
    assert out["coverage_status"] == "pending_next_run"


def test_ragas_eval_snapshot_ok_when_expanded_eval_set_is_consumed(tmp_path, monkeypatch):
    report = tmp_path / "eval-report-ragas.json"
    report.write_text(
        json.dumps(
            {
                "status": "ok",
                "cases": 12,
                "ragas": {
                    "n": 12,
                    "faithfulness_mean": 0.9,
                    "answer_relevance_mean": 0.8,
                    "answer_source": "generated",
                    "answer_source_counts": {"generated": 12},
                },
            }
        )
    )
    eval_set = tmp_path / "eval_set_ragas_answers.json"
    eval_set.write_text(json.dumps([{"query": str(i)} for i in range(12)]))
    monkeypatch.setattr(ops_readiness, "RAGAS_EVAL_REPORT", report)
    monkeypatch.setattr(ops_readiness, "RAGAS_ANSWER_EVAL_SET", eval_set)

    out = ops_readiness.ragas_eval_snapshot()

    assert out["status"] == "ok"
    assert out["case_count"] == 12
    assert out["eval_set_case_count"] == 12
    assert out["generated_target_cases"] == 12
    assert out["coverage_status"] == "ok"


def test_readiness_snapshot_blocks_insufficient_outcome_maturity(monkeypatch):
    monkeypatch.setattr(ops_readiness, "backup_restore_snapshot", lambda: {"all_ok": True, "status": "ok"})
    monkeypatch.setattr(ops_readiness, "retrieval_regression_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "crag_regression_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "crag_correction_regression_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "crag_llm_correction_regression_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "adversarial_eval_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "holdout_eval_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "ragas_eval_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "release_readiness_snapshot", lambda: {"status": "ok", "blockers": []})
    monkeypatch.setattr(ops_readiness, "ui_parity_audit_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "openclaw_gateway_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "source_governance_readiness_snapshot", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "remediation_incident_ledger", lambda: {"status": "ok"})
    monkeypatch.setattr(ops_readiness, "slo_escalation_ledger", lambda: {"status": "ok"})
    monkeypatch.setattr(
        ops_readiness,
        "skill_promotion_readiness_snapshot",
        lambda: {
            "status": "ok",
            "outcome_maturity": {"status": "insufficient_data", "readiness_blocking": True},
        },
    )
    monkeypatch.setattr(
        ops_readiness,
        "failure_lesson_outcome_readiness_snapshot",
        lambda: {"status": "insufficient_data", "readiness_blocking": True},
    )

    out = ops_readiness.readiness_snapshot()

    assert out["status"] == "blocked"
    assert "skill_promotion_outcomes" in out["blockers"]
    assert "failure_lesson_outcome" in out["blockers"]
