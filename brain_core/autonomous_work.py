"""Read-only autonomous/background work visibility.

This module answers Chris's core observability question: what did Brain actually
run without a synchronous human approval click?  It does not trigger work.  It
normalizes existing durable ledgers into one feed:

- task_dispatch_attempts: agent task execution attempts/results
- slo_remediation.jsonl: deterministic SLO remediation actions
- autonomy_decisions: recent L2/L3 no-ack authorization decisions

The companion SLO counts malformed concrete work records so hidden/background
work cannot silently lose the evidence needed for UI and postmortems.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

try:
    from config import AUTONOMY_DB, BRAIN_LOGS_DIR
except ImportError:  # pragma: no cover - direct script fallback
    BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")
    AUTONOMY_DB = BRAIN_LOGS_DIR / "autonomy.db"

SLO_REMEDIATION_LOG = BRAIN_LOGS_DIR / "slo_remediation.jsonl"
DEFAULT_WINDOW_HOURS = 24


def _parse_dt(raw: Any) -> datetime | None:
    if raw in (None, ""):
        return None
    try:
        text = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except ValueError:
        return None


def _now() -> datetime:
    return datetime.now(UTC)


def _window_start(hours: int) -> datetime:
    return _now() - timedelta(hours=max(1, int(hours or DEFAULT_WINDOW_HOURS)))


def _load_json(raw: Any, default: Any) -> Any:
    if isinstance(raw, dict | list):
        return raw
    if not isinstance(raw, str) or not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


def _read_jsonl_tail(path: Path, limit: int = 200) -> list[dict[str, Any]]:
    if limit <= 0 or not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines[-max(limit * 4, limit) :]:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows[-limit:]


def _consent_from_task(task_row: sqlite3.Row | None) -> str:
    if task_row is None:
        return "unknown"
    if str(task_row["created_by"] or "") == "chris":
        return "explicit_or_manual_task"
    log = _load_json(task_row["execution_log"], [])
    if isinstance(log, list):
        actors = {str(item.get("by") or "") for item in log if isinstance(item, dict)}
        if "chris" in actors:
            return "explicit_chris_ack"
        if "autopilot" in actors:
            return "autopilot_no_prior_ack"
    return "brain_no_prior_ack"


def _task_evaluation_work(limit: int, since: datetime) -> list[dict[str, Any]]:
    """Return LLM/classifier task-evaluation decisions and Brain actions.

    This fills the visibility gap between "Brain evaluated it" and the later
    dispatch attempt: Chris can see the LLM decision, Brain's routing action,
    reason text, and where to inspect execution evidence.
    """

    if not AUTONOMY_DB.exists():
        return []
    try:
        with sqlite3.connect(str(AUTONOMY_DB)) as conn:
            conn.row_factory = sqlite3.Row
            has_tasks = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'"
            ).fetchone()
            if not has_tasks:
                return []
            rows = conn.execute(
                """SELECT id, title, description, status, assigned_agent,
                          created_by, execution_log, metadata, updated_at
                   FROM tasks
                   WHERE metadata LIKE '%task_evaluation_%'
                   ORDER BY updated_at DESC
                   LIMIT ?""",
                (max(limit * 4, limit),),
            ).fetchall()
    except sqlite3.Error:
        return []

    items: list[dict[str, Any]] = []
    for row in rows:
        metadata = _load_json(row["metadata"], {})
        if not isinstance(metadata, dict):
            continue
        routed_at_raw = metadata.get("task_evaluation_routed_at") or metadata.get("escalation_llm_routed_at")
        routed_at = _parse_dt(routed_at_raw)
        if routed_at is None or routed_at < since:
            continue
        execution_log = _load_json(row["execution_log"], [])
        reason = str(
            metadata.get("task_evaluation_reason")
            or metadata.get("escalation_llm_action")
            or metadata.get("escalation_llm_reason")
            or ""
        )
        action = str(metadata.get("task_evaluation_action") or metadata.get("escalation_llm_route") or "")
        brain_action = str(metadata.get("task_evaluation_brain_action") or action or "handled")
        decision = str(metadata.get("task_evaluation_decision") or metadata.get("escalation_llm_route") or "")
        next_evidence = str(
            metadata.get("task_evaluation_next_evidence") or f"/brain/tasks/{row['id']}/execution"
        )
        items.append(
            {
                "id": f"task_eval:{row['id']}:{routed_at_raw}",
                "source": "tasks.metadata",
                "kind": "task_evaluation",
                "timestamp": routed_at_raw,
                "status": "handled",
                "actor": str(metadata.get("task_evaluation_source") or "task_queue"),
                "action": action,
                "target": row["id"],
                "consent": _consent_from_task(row),
                "trace_id": str(metadata.get("trace_id") or row["id"]),
                "summary": reason,
                "decision": decision,
                "brain_action": brain_action,
                "llm_reason": reason,
                "next_evidence": next_evidence,
                "evidence": {
                    "task_id": row["id"],
                    "task_status": row["status"],
                    "assigned_agent": row["assigned_agent"],
                    "title": row["title"],
                    "metadata": metadata,
                    "execution_log": execution_log,
                },
            }
        )
        if len(items) >= limit:
            break
    return items


def _dispatch_work(limit: int, since: datetime) -> list[dict[str, Any]]:
    if not AUTONOMY_DB.exists():
        return []
    try:
        with sqlite3.connect(str(AUTONOMY_DB)) as conn:
            conn.row_factory = sqlite3.Row
            has_attempts = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_dispatch_attempts'"
            ).fetchone()
            if not has_attempts:
                return []
            rows = conn.execute(
                """SELECT a.*, t.title, t.description, t.status AS task_status,
                          t.created_by, t.execution_log, t.metadata AS task_metadata
                   FROM task_dispatch_attempts a
                   LEFT JOIN tasks t ON t.id = a.task_id
                   WHERE a.started_at >= ? OR a.completed_at >= ?
                   ORDER BY COALESCE(a.completed_at, a.started_at) DESC
                   LIMIT ?""",
                (since.isoformat(timespec="seconds"), since.isoformat(timespec="seconds"), limit),
            ).fetchall()
    except sqlite3.Error:
        return []

    items: list[dict[str, Any]] = []
    for row in rows:
        metadata = _load_json(row["metadata"], {})
        task_meta = _load_json(row["task_metadata"], {})
        completed_at = row["completed_at"] or ""
        started_at = row["started_at"] or ""
        items.append(
            {
                "id": row["id"],
                "source": "task_dispatch_attempts",
                "kind": "task_dispatch",
                "timestamp": completed_at or started_at,
                "status": row["status"],
                "actor": row["agent"] or "agent",
                "action": row["title"] or row["task_id"],
                "target": row["task_id"],
                "consent": _consent_from_task(row),
                "trace_id": row["trace_id"],
                "summary": row["result_preview"] or row["error"] or row["description"] or "",
                "evidence": {
                    "attempt_id": row["id"],
                    "attempt_no": row["attempt_no"],
                    "backend": row["backend"],
                    "model": row["model"],
                    "task_status": row["task_status"],
                    "duration_ms": row["duration_ms"],
                    "error_class": row["error_class"],
                    "metadata": metadata,
                    "task_metadata": task_meta,
                },
            }
        )
    return items


def _slo_work(limit: int, since: datetime) -> list[dict[str, Any]]:
    rows = _read_jsonl_tail(SLO_REMEDIATION_LOG, limit=max(limit, 200))
    items: list[dict[str, Any]] = []
    for row in reversed(rows):
        timestamp = _parse_dt(row.get("timestamp"))
        if timestamp is None or timestamp < since:
            continue
        kind = str(row.get("kind") or "slo_remediation")
        status = str(row.get("status") or "unknown")
        items.append(
            {
                "id": f"slo:{row.get('slo', 'unknown')}:{row.get('timestamp', '')}:{row.get('action', '')}",
                "source": "slo_remediation.jsonl",
                "kind": f"slo_{kind}",
                "timestamp": row.get("timestamp"),
                "status": status,
                "actor": "slo_remediation",
                "action": row.get("action") or row.get("reason") or "unknown",
                "target": row.get("slo") or "unknown",
                "consent": "slo_policy_no_prior_ack" if kind in {"trigger", "config"} else "manual_required",
                "trace_id": str(row.get("pid") or row.get("timestamp") or ""),
                "summary": row.get("reason") or "",
                "evidence": row,
            }
        )
        if len(items) >= limit:
            break
    return items


def _authorization_work(limit: int, since: datetime) -> list[dict[str, Any]]:
    if not AUTONOMY_DB.exists():
        return []
    try:
        with sqlite3.connect(str(AUTONOMY_DB)) as conn:
            conn.row_factory = sqlite3.Row
            has_decisions = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='autonomy_decisions'"
            ).fetchone()
            if not has_decisions:
                return []
            rows = conn.execute(
                """SELECT * FROM autonomy_decisions
                   WHERE ts_utc >= ? AND allowed = 1 AND level IN ('L2', 'L3')
                   ORDER BY ts_utc DESC
                   LIMIT ?""",
                (since.isoformat(timespec="seconds"), limit),
            ).fetchall()
    except sqlite3.Error:
        return []

    items: list[dict[str, Any]] = []
    for row in rows:
        context = _load_json(row["context_json"], {})
        if not isinstance(context, dict):
            context = {"raw_context": context}
        items.append(
            {
                "id": f"auth:{row['id']}",
                "source": "autonomy_decisions",
                "kind": "authorization_no_prior_ack",
                "timestamp": row["ts_utc"],
                "status": "allowed",
                "actor": "autonomy_gate",
                "action": row["kind"],
                "target": str(context.get("task_id") or context.get("trigger_id") or ""),
                "consent": "notify_then_act" if row["level"] == "L2" else "immediate_no_prior_ack",
                "trace_id": str(context.get("task_id") or context.get("trace_id") or row["id"]),
                "summary": row["reason"],
                "evidence": {
                    "level": row["level"],
                    "breaker_state": row["breaker_state"],
                    "context": context,
                },
            }
        )
    return items


def recent_autonomous_work(limit: int = 50, hours: int = DEFAULT_WINDOW_HOURS) -> dict[str, Any]:
    """Return a normalized feed of actual and authorized autonomous work."""

    limit = max(1, min(int(limit or 50), 200))
    since = _window_start(hours)
    concrete_items = [
        *_task_evaluation_work(limit, since),
        *_dispatch_work(limit, since),
        *_slo_work(limit, since),
    ]
    authorization_items = _authorization_work(limit, since)
    concrete_items.sort(
        key=lambda item: _parse_dt(item.get("timestamp")) or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    authorization_items.sort(
        key=lambda item: _parse_dt(item.get("timestamp")) or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    # Concrete work answers "what actually ran"; authorization decisions are
    # still useful consent evidence, but should not flood the top of the feed.
    items = [*concrete_items, *authorization_items]
    items = items[:limit]
    by_kind: dict[str, int] = {}
    by_consent: dict[str, int] = {}
    for item in items:
        by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
        by_consent[item["consent"]] = by_consent.get(item["consent"], 0) + 1
    gaps = visibility_gap_count(hours=hours)
    return {
        "generated_at": _now().isoformat(timespec="seconds"),
        "window_hours": max(1, int(hours or DEFAULT_WINDOW_HOURS)),
        "total": len(items),
        "status": "ok" if gaps == 0 else "visibility_gap",
        "visibility_gap_count": gaps,
        "by_kind": by_kind,
        "by_consent": by_consent,
        "items": items,
    }


def _dispatch_visibility_gaps(since: datetime) -> int:
    if not AUTONOMY_DB.exists():
        return 0
    try:
        with sqlite3.connect(str(AUTONOMY_DB)) as conn:
            has_attempts = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_dispatch_attempts'"
            ).fetchone()
            if not has_attempts:
                return 0
            row = conn.execute(
                """SELECT COUNT(*) FROM task_dispatch_attempts
                   WHERE started_at >= ?
                     AND status IN ('completed', 'failed', 'deferred')
                     AND (
                       COALESCE(task_id, '') = ''
                       OR COALESCE(trace_id, '') = ''
                       OR COALESCE(completed_at, '') = ''
                     )""",
                (since.isoformat(timespec="seconds"),),
            ).fetchone()
            return int(row[0] or 0) if row else 0
    except sqlite3.Error:
        return 0


def _slo_visibility_gaps(since: datetime) -> int:
    gaps = 0
    for row in _read_jsonl_tail(SLO_REMEDIATION_LOG, limit=500):
        timestamp = _parse_dt(row.get("timestamp"))
        if timestamp is None or timestamp < since:
            continue
        required = ("timestamp", "slo", "kind", "action", "status")
        if any(not row.get(key) for key in required):
            gaps += 1
        if row.get("kind") == "trigger" and row.get("status") == "ok" and not row.get("pid"):
            gaps += 1
    return gaps


def _task_evaluation_visibility_gaps(since: datetime) -> int:
    if not AUTONOMY_DB.exists():
        return 0
    try:
        with sqlite3.connect(str(AUTONOMY_DB)) as conn:
            conn.row_factory = sqlite3.Row
            has_tasks = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'"
            ).fetchone()
            if not has_tasks:
                return 0
            rows = conn.execute(
                """SELECT metadata FROM tasks
                   WHERE metadata LIKE '%task_evaluation_%'"""
            ).fetchall()
    except sqlite3.Error:
        return 0
    gaps = 0
    for row in rows:
        metadata = _load_json(row["metadata"], {})
        if not isinstance(metadata, dict):
            continue
        routed_at = _parse_dt(metadata.get("task_evaluation_routed_at"))
        if routed_at is None or routed_at < since:
            continue
        # Older task-evaluation records can derive decision/brain_action/evidence
        # from existing action/source fields. Count only records that lack the
        # irreducible evidence needed to explain what happened.
        required = (
            "task_evaluation_action",
            "task_evaluation_reason",
            "task_evaluation_source",
            "task_evaluation_routed_at",
        )
        if any(not metadata.get(key) for key in required):
            gaps += 1
    return gaps


def visibility_gap_count(hours: int = DEFAULT_WINDOW_HOURS) -> int:
    """Count concrete background work records lacking postmortem evidence."""

    since = _window_start(hours)
    return (
        _task_evaluation_visibility_gaps(since)
        + _dispatch_visibility_gaps(since)
        + _slo_visibility_gaps(since)
    )


def readiness_snapshot(hours: int = DEFAULT_WINDOW_HOURS, limit: int = 20) -> dict[str, Any]:
    feed = recent_autonomous_work(limit=limit, hours=hours)
    return {
        "status": "ok" if feed["visibility_gap_count"] == 0 else "blocked",
        "readiness_blocking": feed["visibility_gap_count"] > 0,
        "window_hours": feed["window_hours"],
        "total": feed["total"],
        "visibility_gap_count": feed["visibility_gap_count"],
        "by_kind": feed["by_kind"],
        "by_consent": feed["by_consent"],
        "recent": feed["items"][:5],
    }
