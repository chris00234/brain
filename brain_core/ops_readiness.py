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
RETRIEVAL_REGRESSION_LOG = LOGS_DIR / "retrieval_regression.json"


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


def retrieval_regression_snapshot() -> dict[str, Any]:
    return _read_json(RETRIEVAL_REGRESSION_LOG)


def backup_restore_snapshot() -> dict[str, Any]:
    return _read_json(BACKUP_RESTORE_DRILL)


def readiness_snapshot() -> dict[str, Any]:
    backup = backup_restore_snapshot()
    retrieval = retrieval_regression_snapshot()
    release = release_readiness_snapshot()
    incidents = remediation_incident_ledger()
    escalations = slo_escalation_ledger()
    blockers: list[str] = []
    if backup.get("all_ok") is False or backup.get("status") in {"missing", "error"}:
        blockers.append("backup_restore_drill")
    if retrieval.get("status") in {"missing", "error", "breached"}:
        blockers.append("retrieval_regression")
    if release.get("status") in {"missing", "error", "blocked"}:
        blockers.append("release_readiness")
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "status": "ready" if not blockers else "blocked",
        "blockers": blockers,
        "backup_restore_drill": backup,
        "retrieval_regression": retrieval,
        "release_readiness": release,
        "slo_incident_ledger": incidents,
        "slo_escalation_ledger": escalations,
    }
