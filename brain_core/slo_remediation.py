"""Deterministic SLO auto-remediation playbook.

This module is intentionally LLM-free. It only runs known-safe, reversible
actions for SLOs where the recovery step is mechanical: drain a queue, run a
scheduled backup/audit job, or set a short-lived throttle flag.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

log = logging.getLogger("brain.slo_remediation")

BRAIN_ROOT = Path("/Users/chrischo/server/brain")
LOG_FILE = BRAIN_ROOT / "logs" / "slo_remediation.jsonl"
ESCALATION_LOG_FILE = BRAIN_ROOT / "logs" / "slo_escalations.jsonl"
REMEDIATION_FLOOR_S = 30 * 60

ActionKind = Literal["trigger", "config", "manual"]


@dataclass(frozen=True)
class RemediationRule:
    slo: str
    kind: ActionKind
    threshold: float
    action: str
    reason: str
    config_value: str | None = None
    ttl_s: int | None = None


PLAYBOOK: dict[str, RemediationRule] = {
    "breaker_open_count": RemediationRule(
        slo="breaker_open_count",
        kind="manual",
        threshold=0,
        action="inspect /brain/breakers and reset only after a successful provider probe",
        reason="A circuit breaker is open; automatic reset would defeat backoff protection.",
    ),
    "outbox_pending_count": RemediationRule(
        slo="outbox_pending_count",
        kind="trigger",
        threshold=20,
        action="outbox_drain",
        reason="SessionEnd outbox backlog exceeded budget; drain pending envelopes.",
    ),
    "llm_backlog_pending": RemediationRule(
        slo="llm_backlog_pending",
        kind="trigger",
        threshold=200,
        action="llm_backlog_drain",
        reason="Legacy LLM backlog pending count exceeded budget; drain queue.",
    ),
    "telegram_backlog_pending_count": RemediationRule(
        slo="telegram_backlog_pending_count",
        kind="trigger",
        threshold=0,
        action="llm_backlog_drain",
        reason="Direct Telegram alert backlog must be replayed immediately.",
    ),
    "logs_dir_total_mb": RemediationRule(
        slo="logs_dir_total_mb",
        kind="trigger",
        threshold=2048,
        action="log_rotation",
        reason="Brain logs exceeded size budget; run retention/cleanup pass.",
    ),
    "entry_contract_missing_pct": RemediationRule(
        slo="entry_contract_missing_pct",
        kind="trigger",
        threshold=0,
        action="entry_contract_audit",
        reason="Entry contract drift detected; run live audit for exact offending collections.",
    ),
    "qdrant_backup_age_hours": RemediationRule(
        slo="qdrant_backup_age_hours",
        kind="trigger",
        threshold=36,
        action="qdrant_backup",
        reason="Qdrant backup is stale; trigger the backup job now.",
    ),
    "neo4j_backup_age_hours": RemediationRule(
        slo="neo4j_backup_age_hours",
        kind="trigger",
        threshold=36,
        action="neo4j_backup",
        reason="Neo4j graph backup is stale; trigger the backup job now.",
    ),
    "backup_restore_drill_age_hours": RemediationRule(
        slo="backup_restore_drill_age_hours",
        kind="trigger",
        threshold=192,
        action="backup_restore_drill",
        reason="Backup restore drill is stale; run restore-readiness verification now.",
    ),
    "brain_server_rss_mb": RemediationRule(
        slo="brain_server_rss_mb",
        kind="config",
        threshold=2700,
        action="BRAIN_SCHED_MAX_HEAVY_JOBS",
        config_value="0",
        ttl_s=1800,
        reason="Brain server RSS is near ceiling; pause heavy scheduler work for 30 minutes.",
    ),
    "telegram_direct_health": RemediationRule(
        slo="telegram_direct_health",
        kind="manual",
        threshold=0,
        action="check TELEGRAM_JENNA_TOKEN/chat reachability",
        reason="Direct Telegram healthcheck failed; token/network/chat auth require external repair.",
    ),
    "openclaw_gateway_health": RemediationRule(
        slo="openclaw_gateway_health",
        kind="trigger",
        threshold=0,
        action="openclaw_gateway_start",
        reason="OpenClaw gateway is unreachable; start the local gateway so approved agent handoff tasks can actually execute.",
    ),
    "task_dispatch_stale_started_count": RemediationRule(
        slo="task_dispatch_stale_started_count",
        kind="manual",
        threshold=0,
        action="inspect /brain/task-dispatch-attempts and requeue or close stale dispatch evidence",
        reason="A task dispatch attempt stayed in started too long; automatic closure would obscure execution truth.",
    ),
}


def _disabled() -> bool:
    return os.environ.get("BRAIN_SLO_AUTOREMEDIATE", "on").lower() in {"off", "0", "false", "no"}


def _append_jsonl(path: Path, records: list[dict]) -> None:
    if not records:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            for rec in records:
                fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception as exc:
        log.debug("slo jsonl append failed path=%s: %s", path, exc)


def _append_log(records: list[dict]) -> None:
    _append_jsonl(LOG_FILE, records)


def _append_escalations(records: list[dict]) -> None:
    _append_jsonl(ESCALATION_LOG_FILE, records)


def _escalation_record(base: dict, *, route: str, status: str, detail: dict | None = None) -> dict:
    """Build a durable escalation ledger row for SLOs automation cannot close.

    SLO remediation already writes every action to ``slo_remediation.jsonl``.
    This second ledger is narrower: it records items that need an escalation
    lane after deterministic automation is exhausted or unsafe.  Human-routed
    rows are for credential/manual-authority blockers; LLM-routed rows are for
    follow-up debugging jobs/agents.
    """

    return {
        "timestamp": base["timestamp"],
        "source": "slo_remediation",
        "slo": base["slo"],
        "current": base["current"],
        "threshold": base["threshold"],
        "route": route,
        "status": "open",
        "escalation_status": status,
        "action": base["action"],
        "reason": base["reason"],
        "detail": detail or {},
    }


def _rate_key(rule: RemediationRule) -> str:
    return f"slo_remediation.{rule.slo}.{rule.action}.last_at"


def _should_fire(rule: RemediationRule) -> bool:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import brain_config_store

        last = brain_config_store.get(_rate_key(rule))
        return not last or (time.time() - float(last)) >= REMEDIATION_FLOOR_S
    except Exception:
        return True


def _record_fire(rule: RemediationRule) -> None:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import brain_config_store

        brain_config_store.set(_rate_key(rule), f"{time.time():.3f}", updated_by="slo_remediation")
    except Exception as exc:
        log.debug("slo remediation rate marker failed: %s", exc)


def _trigger(job_name: str) -> dict:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from job_registry import dispatch_job

        pid = dispatch_job(job_name)
        return {"status": "ok", "pid": pid}
    except Exception as exc:
        log.warning("slo remediation trigger %s failed: %s", job_name, exc)
        return {"status": "error", "error": str(exc)[:300]}


def _set_throttle(rule: RemediationRule) -> list[dict]:
    assert rule.config_value is not None
    actions: list[dict] = []
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import brain_config_store

        brain_config_store.set(rule.action, rule.config_value, updated_by="slo_remediation")
        actions.append({"status": "ok", "action": f"config:{rule.action}={rule.config_value}"})
        if rule.ttl_s:
            until_key = "BRAIN_SCHED_HEAVY_THROTTLE_UNTIL"
            until_value = str(int(time.time() + rule.ttl_s))
            brain_config_store.set(until_key, until_value, updated_by="slo_remediation")
            actions.append({"status": "ok", "action": f"config:{until_key}={until_value}"})
    except Exception as exc:
        log.warning("slo remediation config %s failed: %s", rule.action, exc)
        actions.append({"status": "error", "action": f"config:{rule.action}", "error": str(exc)[:300]})
    return actions


def recent_actions(limit: int = 20) -> list[dict]:
    """Return the most recent SLO remediation log records, newest last.

    This is best-effort observability for health/metrics endpoints; malformed
    rows are skipped so one partial write cannot break liveness probes.
    """
    if limit <= 0 or not LOG_FILE.exists():
        return []
    try:
        lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict] = []
    for line in lines[-max(limit * 3, limit) :]:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows[-limit:]


def apply_direct_remediations(violations: list[dict]) -> dict:
    """Apply safe deterministic remediations for current SLO violations.

    Returns a structured summary and appends all fired/manual actions to
    logs/slo_remediation.jsonl so remediation itself is auditable.
    """
    if _disabled():
        return {"skipped": "disabled_via_env"}

    records: list[dict] = []
    escalations: list[dict] = []
    actions: list[dict] = []
    now = datetime.now(UTC).isoformat(timespec="seconds")

    for violation in violations:
        slo_name = str(violation.get("slo") or "")
        rule = PLAYBOOK.get(slo_name)
        if not rule:
            continue
        try:
            current = float(violation.get("current") or 0.0)
        except (TypeError, ValueError):
            current = 0.0
        if current <= rule.threshold:
            continue

        base = {
            "timestamp": now,
            "slo": slo_name,
            "current": current,
            "threshold": rule.threshold,
            "kind": rule.kind,
            "action": rule.action,
            "reason": rule.reason,
        }

        if rule.kind == "trigger":
            if not _should_fire(rule):
                outcome = {"status": "rate_limited"}
            else:
                outcome = _trigger(rule.action)
                if outcome.get("status") == "ok":
                    _record_fire(rule)
                else:
                    escalations.append(
                        _escalation_record(
                            base,
                            route="llm",
                            status="trigger_failed",
                            detail={"outcome": outcome},
                        )
                    )
            rec = {**base, **outcome}
            actions.append({"slo": slo_name, "action": f"trigger:{rule.action}", **outcome})
            records.append(rec)
        elif rule.kind == "config":
            if not _should_fire(rule):
                rec = {**base, "status": "rate_limited"}
                actions.append({"slo": slo_name, "action": f"config:{rule.action}", "status": "rate_limited"})
                records.append(rec)
            else:
                fired_ok = False
                for outcome in _set_throttle(rule):
                    rec = {**base, **outcome}
                    fired_ok = fired_ok or outcome.get("status") == "ok"
                    if outcome.get("status") == "error":
                        escalations.append(
                            _escalation_record(
                                base,
                                route="llm",
                                status="config_failed",
                                detail={"outcome": outcome},
                            )
                        )
                    actions.append({"slo": slo_name, **outcome})
                    records.append(rec)
                if fired_ok:
                    _record_fire(rule)
        else:
            if not _should_fire(rule):
                rec = {**base, "status": "rate_limited"}
                status = "rate_limited"
            else:
                rec = {**base, "status": "manual_required"}
                status = "manual_required"
                _record_fire(rule)
                escalations.append(
                    _escalation_record(
                        base,
                        route="human",
                        status="manual_required",
                        detail={"requires_human": True},
                    )
                )
            action = {"slo": slo_name, "action": f"manual:{rule.action}", "status": status}
            if status == "manual_required":
                action["escalated"] = True
                action["escalation_log"] = str(ESCALATION_LOG_FILE)
            actions.append(action)
            records.append(rec)

    _append_log(records)
    _append_escalations(escalations)
    if actions:
        log.info("slo direct remediations fired: %s", actions)
    return {"actions": actions}
