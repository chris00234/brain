"""Operational readiness aggregation for Brain observability surfaces.

This module is read-only and intentionally avoids sending alerts or triggering
jobs. It turns existing JSON/JSONL audit artifacts into a compact status model
for API/UI, release checks, and code review evidence.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BRAIN_ROOT = Path("/Users/chrischo/server/brain")
LOGS_DIR = BRAIN_ROOT / "logs"
BACKUP_RESTORE_DRILL = LOGS_DIR / "backup_restore_drill.json"
SLO_REMEDIATION_LOG = LOGS_DIR / "slo_remediation.jsonl"
SLO_ESCALATION_LOG = LOGS_DIR / "slo_escalations.jsonl"
RELEASE_READINESS_LOG = LOGS_DIR / "release_readiness.json"
UI_PARITY_AUDIT_LOG = LOGS_DIR / "ui_parity_audit.json"
RETRIEVAL_REGRESSION_LOG = LOGS_DIR / "retrieval_regression.json"
CRAG_REGRESSION_LOG = LOGS_DIR / "crag_regression.json"
CRAG_CORRECTION_REGRESSION_LOG = LOGS_DIR / "crag_correction_regression.json"
CRAG_LLM_CORRECTION_REGRESSION_LOG = LOGS_DIR / "crag_llm_correction_regression.json"
RAGAS_EVAL_REPORT = LOGS_DIR / "eval-report-ragas.json"
RAGAS_ANSWER_EVAL_SET = BRAIN_ROOT / "cli" / "eval_set_ragas_answers.json"
ADVERSARIAL_EVAL_REPORT = LOGS_DIR / "eval-report-adversarial.json"
HOLDOUT_EVAL_REPORT = LOGS_DIR / "eval-report-holdout.json"
RAGAS_FAITHFULNESS_MIN = 0.7
RAGAS_GENERATED_ANSWER_RELEVANCE_MIN = 0.6
RAGAS_GENERATED_MIN_CASES = 8
RAGAS_GENERATED_TARGET_CASES = 12
ADVERSARIAL_EVAL_MIN_ACCURACY = 80.0
ADVERSARIAL_NEGATIVE_PASS_MIN = 100.0
CRAG_SAFETY_RATE_MIN = 100.0
CRAG_CORRECTION_RECOVERY_RATE_MIN = 80.0
CRAG_CORRECTION_MIN_CASES = 3
HOLDOUT_EVAL_MIN_ACCURACY = 90.0
HOLDOUT_NEGATIVE_PASS_MIN = 100.0
HOLDOUT_MIN_CASES = 10


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"status": "missing", "path": str(path)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "error", "path": str(path), "error": str(exc)[:200]}
    if isinstance(data, dict):
        data.setdefault("status", "ok" if data.get("all_ok", True) else "error")
        data.setdefault("path", str(path))
        return data
    return {"status": "error", "path": str(path), "error": "json_root_not_object"}


def _read_jsonl_tail(path: Path, limit: int = 50) -> list[dict[str, Any]]:
    if limit <= 0 or not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines[-max(limit * 3, limit) :]:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows[-limit:]


def remediation_incident_ledger(limit: int = 200) -> dict[str, Any]:
    """Group recent SLO remediation records into an incident ledger."""
    rows = _read_jsonl_tail(SLO_REMEDIATION_LOG, limit=limit)
    by_slo: dict[str, Counter[str]] = defaultdict(Counter)
    last_by_slo: dict[str, dict[str, Any]] = {}
    for row in rows:
        slo = str(row.get("slo") or "unknown")
        status = str(row.get("status") or "unknown")
        by_slo[slo][status] += 1
        last_by_slo[slo] = row
    return {
        "status": "ok",
        "total_recent": len(rows),
        "by_slo": {slo: dict(counts) for slo, counts in sorted(by_slo.items())},
        "last_by_slo": last_by_slo,
        "recent": rows[-20:],
    }


def slo_escalation_ledger(limit: int = 100) -> dict[str, Any]:
    """Return recent SLO escalation rows created by deterministic remediation."""

    rows = _read_jsonl_tail(SLO_ESCALATION_LOG, limit=limit)
    by_route: Counter[str] = Counter()
    by_slo: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        route = str(row.get("route") or "unknown")
        slo = str(row.get("slo") or "unknown")
        status = str(row.get("escalation_status") or row.get("status") or "unknown")
        by_route[route] += 1
        by_slo[slo][status] += 1
    return {
        "status": "ok",
        "total_recent": len(rows),
        "by_route": dict(by_route),
        "by_slo": {slo: dict(counts) for slo, counts in sorted(by_slo.items())},
        "recent": rows[-20:],
    }


def release_readiness_snapshot() -> dict[str, Any]:
    return _read_json(RELEASE_READINESS_LOG)


def ui_parity_audit_snapshot() -> dict[str, Any]:
    data = _read_json(UI_PARITY_AUDIT_LOG)
    if data.get("status") in {"missing", "error"}:
        return data
    status = str(data.get("status") or "ok")
    blocked = int(data.get("blocked") or 0)
    required = int(data.get("required") or 0)
    ok = int(data.get("ok") or 0)
    if status != "ok" or blocked > 0 or (required > 0 and ok < required):
        status = "blocked"
    return {
        **data,
        "status": status,
        "min_required": 11,
    }


def retrieval_regression_snapshot() -> dict[str, Any]:
    return _read_json(RETRIEVAL_REGRESSION_LOG)


def crag_regression_snapshot() -> dict[str, Any]:
    data = _read_json(CRAG_REGRESSION_LOG)
    if data.get("status") in {"missing", "error"}:
        return data
    safety_rate = float(data.get("safety_rate") or 0.0)
    false_accepts = int(data.get("dangerous_false_accepts") or 0)
    status = str(data.get("status") or "ok")
    if false_accepts > 0 or safety_rate < CRAG_SAFETY_RATE_MIN:
        status = "breached"
    return {
        **data,
        "status": status,
        "min_safety_rate": max(float(data.get("min_safety_rate") or 0.0), CRAG_SAFETY_RATE_MIN),
    }


def crag_correction_regression_snapshot() -> dict[str, Any]:
    data = _read_json(CRAG_CORRECTION_REGRESSION_LOG)
    if data.get("status") in {"missing", "error"}:
        return data
    recovery_rate = float(data.get("recovery_rate") or 0.0)
    recovery_needed = int(data.get("recovery_needed") or 0)
    failed_recoveries = int(data.get("failed_recoveries") or 0)
    status = str(data.get("status") or "ok")
    if recovery_needed < CRAG_CORRECTION_MIN_CASES:
        status = "insufficient_coverage"
    elif failed_recoveries > 0 or recovery_rate < CRAG_CORRECTION_RECOVERY_RATE_MIN:
        status = "breached"
    return {
        **data,
        "status": status,
        "min_recovery_rate": max(
            float(data.get("min_recovery_rate") or 0.0), CRAG_CORRECTION_RECOVERY_RATE_MIN
        ),
        "min_recovery_cases": max(int(data.get("min_recovery_cases") or 0), CRAG_CORRECTION_MIN_CASES),
    }


def crag_llm_correction_regression_snapshot() -> dict[str, Any]:
    data = _read_json(CRAG_LLM_CORRECTION_REGRESSION_LOG)
    if data.get("status") in {"missing", "error"}:
        return {**data, "readiness_blocking": False, "coverage_level": "exploratory"}
    recovery_rate = float(data.get("recovery_rate") or 0.0)
    failed_recoveries = int(data.get("failed_recoveries") or 0)
    status = str(data.get("status") or "ok")
    if failed_recoveries > 0 or recovery_rate < CRAG_CORRECTION_RECOVERY_RATE_MIN:
        status = "breached"
    return {
        **data,
        "status": status,
        "readiness_blocking": False,
        "coverage_level": "exploratory",
        "min_recovery_rate": max(
            float(data.get("min_recovery_rate") or 0.0), CRAG_CORRECTION_RECOVERY_RATE_MIN
        ),
    }


def adversarial_eval_snapshot() -> dict[str, Any]:
    data = _read_json(ADVERSARIAL_EVAL_REPORT)
    if data.get("status") in {"missing", "error"}:
        return data
    accuracy = float(data.get("accuracy") or 0.0)
    source_accuracy = float(data.get("source_accuracy") or 0.0)
    v2 = data.get("v2") if isinstance(data.get("v2"), dict) else {}
    negative_pass_pct = v2.get("negative_pass_pct")
    forbidden_hit_count = int(v2.get("forbidden_hit_count") or 0)
    status = "ok"
    if (
        accuracy < ADVERSARIAL_EVAL_MIN_ACCURACY
        or source_accuracy < ADVERSARIAL_EVAL_MIN_ACCURACY
        or (negative_pass_pct is not None and float(negative_pass_pct) < ADVERSARIAL_NEGATIVE_PASS_MIN)
    ):
        status = "breached"
    return {
        **data,
        "status": status,
        "min_accuracy": ADVERSARIAL_EVAL_MIN_ACCURACY,
        "negative_pass_pct": negative_pass_pct,
        "negative_pass_min": ADVERSARIAL_NEGATIVE_PASS_MIN,
        "forbidden_hit_count": forbidden_hit_count,
    }


def holdout_eval_snapshot() -> dict[str, Any]:
    data = _read_json(HOLDOUT_EVAL_REPORT)
    if data.get("status") in {"missing", "error"}:
        return data
    accuracy = float(data.get("accuracy") or 0.0)
    source_accuracy = float(data.get("source_accuracy") or 0.0)
    v2 = data.get("v2") if isinstance(data.get("v2"), dict) else {}
    total = int(v2.get("total") or data.get("passed", 0) + data.get("failed", 0) or 0)
    negative_pass_pct = v2.get("negative_pass_pct")
    forbidden_hit_count = int(v2.get("forbidden_hit_count") or 0)
    status = "ok"
    if (
        total < HOLDOUT_MIN_CASES
        or accuracy < HOLDOUT_EVAL_MIN_ACCURACY
        or source_accuracy < HOLDOUT_EVAL_MIN_ACCURACY
        or (negative_pass_pct is not None and float(negative_pass_pct) < HOLDOUT_NEGATIVE_PASS_MIN)
    ):
        status = "breached"
    return {
        **data,
        "status": status,
        "total": total,
        "min_cases": HOLDOUT_MIN_CASES,
        "min_accuracy": HOLDOUT_EVAL_MIN_ACCURACY,
        "negative_pass_pct": negative_pass_pct,
        "negative_pass_min": HOLDOUT_NEGATIVE_PASS_MIN,
        "forbidden_hit_count": forbidden_hit_count,
    }


def _ragas_eval_set_case_count() -> int:
    try:
        data = json.loads(RAGAS_ANSWER_EVAL_SET.read_text(encoding="utf-8"))
    except Exception:
        return 0
    return len(data) if isinstance(data, list) else 0


def ragas_eval_snapshot() -> dict[str, Any]:
    data = _read_json(RAGAS_EVAL_REPORT)
    if data.get("status") in {"missing", "error"}:
        return data
    ragas = data.get("ragas") if isinstance(data.get("ragas"), dict) else {}
    faithfulness = ragas.get("faithfulness_mean")
    relevance = ragas.get("answer_relevance_mean")
    case_count = int(ragas.get("n") or data.get("cases") or 0)
    answer_source = ragas.get("answer_source") or data.get("ragas_answer_source") or "context"
    answer_source_counts = (
        ragas.get("answer_source_counts") if isinstance(ragas.get("answer_source_counts"), dict) else {}
    )
    generated_answer_gate = answer_source == "generated" and answer_source_counts.get("generated", 0) > 0
    eval_set_case_count = _ragas_eval_set_case_count()
    coverage_target_cases = max(RAGAS_GENERATED_TARGET_CASES, eval_set_case_count)
    coverage_status = (
        "ok" if not generated_answer_gate or case_count >= coverage_target_cases else "pending_next_run"
    )
    status = "ok"
    if faithfulness is None:
        status = "missing_metrics"
    elif float(faithfulness) < RAGAS_FAITHFULNESS_MIN:
        status = "breached"
    elif generated_answer_gate and relevance is None:
        status = "missing_metrics"
    elif (
        (generated_answer_gate and case_count < RAGAS_GENERATED_MIN_CASES)
        or (generated_answer_gate and case_count < coverage_target_cases)
        or (generated_answer_gate and float(relevance) < RAGAS_GENERATED_ANSWER_RELEVANCE_MIN)
    ):
        status = "breached"
    if relevance is None:
        relevance_status = "unknown"
    elif float(relevance) >= RAGAS_GENERATED_ANSWER_RELEVANCE_MIN:
        relevance_status = "ok"
    else:
        relevance_status = "low" if generated_answer_gate else "low_info"
    return {
        **data,
        "status": status,
        "faithfulness_mean": faithfulness,
        "answer_relevance_mean": relevance,
        "answer_relevance_status": relevance_status,
        "answer_source": answer_source,
        "generated_answer_gate": generated_answer_gate,
        "case_count": case_count,
        "generated_min_cases": RAGAS_GENERATED_MIN_CASES,
        "generated_target_cases": coverage_target_cases,
        "eval_set_case_count": eval_set_case_count,
        "coverage_status": coverage_status,
    }


def backup_restore_snapshot() -> dict[str, Any]:
    return _read_json(BACKUP_RESTORE_DRILL)


def skill_promotion_readiness_snapshot() -> dict[str, Any]:
    try:
        from skill_promotion_audit import skill_promotion_audit_snapshot

        return skill_promotion_audit_snapshot()
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:200]}


def source_governance_readiness_snapshot() -> dict[str, Any]:
    try:
        from source_governance import source_governance_snapshot

        return source_governance_snapshot()
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:200]}


def failure_lesson_outcome_readiness_snapshot() -> dict[str, Any]:
    try:
        from failure_lesson_audit import failure_lesson_outcome_snapshot

        return failure_lesson_outcome_snapshot()
    except Exception as exc:
        return {
            "status": "blocked",
            "readiness_blocking": True,
            "error": str(exc)[:200],
        }


def hermes_gateway_snapshot() -> dict[str, Any]:
    """Read-only Hermes gateway readiness derived from the production SLO."""

    try:
        from slos import check_one

        result = check_one("hermes_gateway_health")
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:200]}
    if result is None:
        return {"status": "error", "error": "slo_missing"}
    return {
        "status": "blocked" if result.breached else "ok",
        "actual": result.actual,
        "target": result.slo.target,
        "unit": result.slo.metric_unit,
        "description": result.slo.description,
    }


def autonomous_work_readiness_snapshot() -> dict[str, Any]:
    """Show whether recent autonomous/background work is auditable."""

    try:
        from autonomous_work import readiness_snapshot as _snapshot

        return _snapshot()
    except Exception as exc:
        return {
            "status": "blocked",
            "readiness_blocking": True,
            "error": str(exc)[:200],
        }


def readiness_snapshot() -> dict[str, Any]:
    backup = backup_restore_snapshot()
    retrieval = retrieval_regression_snapshot()
    crag = crag_regression_snapshot()
    crag_correction = crag_correction_regression_snapshot()
    crag_llm_correction = crag_llm_correction_regression_snapshot()
    adversarial_eval = adversarial_eval_snapshot()
    holdout_eval = holdout_eval_snapshot()
    ragas_eval = ragas_eval_snapshot()
    release = release_readiness_snapshot()
    ui_parity = ui_parity_audit_snapshot()
    gateway = hermes_gateway_snapshot()
    source_governance = source_governance_readiness_snapshot()
    skill_promotion = skill_promotion_readiness_snapshot()
    failure_lesson_outcome = failure_lesson_outcome_readiness_snapshot()
    autonomous_work = autonomous_work_readiness_snapshot()
    incidents = remediation_incident_ledger()
    escalations = slo_escalation_ledger()
    blockers: list[str] = []
    if backup.get("all_ok") is False or backup.get("status") in {"missing", "error"}:
        blockers.append("backup_restore_drill")
    if retrieval.get("status") in {"missing", "error", "breached"}:
        blockers.append("retrieval_regression")
    if crag.get("status") in {"missing", "error", "breached"}:
        blockers.append("crag_regression")
    if crag_correction.get("status") in {"missing", "error", "breached", "insufficient_coverage"}:
        blockers.append("crag_correction_regression")
    if adversarial_eval.get("status") in {"missing", "error", "breached"}:
        blockers.append("adversarial_eval")
    if holdout_eval.get("status") in {"missing", "error", "breached"}:
        blockers.append("holdout_eval")
    if ragas_eval.get("status") in {"missing", "error", "missing_metrics", "breached"}:
        blockers.append("ragas_eval")
    if release.get("status") in {"missing", "error", "blocked"}:
        blockers.append("release_readiness")
    if ui_parity.get("status") in {"missing", "error", "blocked"}:
        blockers.append("ui_parity_audit")
    if gateway.get("status") in {"missing", "error", "blocked"}:
        blockers.append("hermes_gateway")
    if source_governance.get("status") in {"missing", "error", "blocked"}:
        blockers.append("source_governance")
    if skill_promotion.get("status") in {"missing", "error", "blocked"}:
        blockers.append("skill_promotion")
    elif (skill_promotion.get("outcome_maturity") or {}).get("readiness_blocking") is True:
        blockers.append("skill_promotion_outcomes")
    if failure_lesson_outcome.get("readiness_blocking") is True:
        blockers.append("failure_lesson_outcome")
    if autonomous_work.get("readiness_blocking") is True:
        blockers.append("autonomous_work_visibility")
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "status": "ready" if not blockers else "blocked",
        "blockers": blockers,
        "backup_restore_drill": backup,
        "retrieval_regression": retrieval,
        "crag_regression": crag,
        "crag_correction_regression": crag_correction,
        "crag_llm_correction_regression": crag_llm_correction,
        "adversarial_eval": adversarial_eval,
        "holdout_eval": holdout_eval,
        "ragas_eval": ragas_eval,
        "release_readiness": release,
        "ui_parity_audit": ui_parity,
        "hermes_gateway": gateway,
        "source_governance": source_governance,
        "skill_promotion": skill_promotion,
        "failure_lesson_outcome": failure_lesson_outcome,
        "autonomous_work": autonomous_work,
        "slo_incident_ledger": incidents,
        "slo_escalation_ledger": escalations,
    }
