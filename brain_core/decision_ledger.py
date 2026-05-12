"""Decision ledger for brain agency decisions.

This is separate from action_audit and autonomy_decisions:
- action_audit records retrieval/tool events.
- autonomy_decisions records gate allow/deny checks.
- decision_ledger records the decision unit: perceived state, options,
  selected action, expected outcome, actual outcome, and review status.

Boundary rule:
- Keep domain-specific quality sidecars (recall_judgments,
  active_recall_judgments, coding_event_outcomes, task outcomes) as local
  telemetry for their pipelines.
- Link those outcomes into decision_ledger only when they resolve a concrete
  decision. Do not duplicate every event here.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

try:
    from brain_core.config import AUTONOMY_DB
except ImportError:  # pragma: no cover - direct execution fallback
    try:
        from config import AUTONOMY_DB
    except ImportError:
        AUTONOMY_DB = Path("/Users/chrischo/server/brain/logs/autonomy.db")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS decision_ledger (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'brain',
    domain TEXT NOT NULL DEFAULT 'general',
    source TEXT NOT NULL DEFAULT '',
    observation_kind TEXT NOT NULL DEFAULT '',
    observation_subject TEXT NOT NULL DEFAULT '',
    perceived_state_json TEXT NOT NULL DEFAULT '{}',
    candidate_options_json TEXT NOT NULL DEFAULT '[]',
    selected_option TEXT NOT NULL DEFAULT '',
    selected_payload_json TEXT NOT NULL DEFAULT '{}',
    confidence REAL NOT NULL DEFAULT 0.0,
    autonomy_level TEXT NOT NULL DEFAULT '',
    expected_outcome TEXT NOT NULL DEFAULT '',
    actual_outcome TEXT NOT NULL DEFAULT '',
    outcome_status TEXT NOT NULL DEFAULT 'pending',
    review_status TEXT NOT NULL DEFAULT 'unreviewed',
    action_audit_id INTEGER,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_decision_ledger_created
  ON decision_ledger(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_decision_ledger_outcome
  ON decision_ledger(outcome_status, review_status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_decision_ledger_obs
  ON decision_ledger(observation_kind, observation_subject, created_at DESC);
"""

log = logging.getLogger("brain.decision_ledger")


@contextmanager
def _conn(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    path = Path(db_path or AUTONOMY_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def record_decision(
    *,
    actor: str = "brain_loop",
    domain: str = "general",
    source: str = "",
    observation_kind: str = "",
    observation_subject: str = "",
    perceived_state: dict | None = None,
    candidate_options: list[dict] | None = None,
    selected_option: str = "",
    selected_payload: dict | None = None,
    confidence: float = 0.0,
    autonomy_level: str = "",
    expected_outcome: str = "",
    actual_outcome: str = "",
    outcome_status: str = "pending",
    review_status: str = "unreviewed",
    action_audit_id: int | None = None,
    dedupe_window_seconds: int | None = None,
    db_path: Path | str | None = None,
) -> str:
    decision_id = f"decision_{uuid.uuid4().hex[:12]}"
    created_at = _now()
    with _conn(db_path) as conn:
        if dedupe_window_seconds:
            existing_id = _find_recent_duplicate(
                conn,
                source=source,
                observation_kind=observation_kind,
                observation_subject=observation_subject,
                selected_option=selected_option,
                window_seconds=dedupe_window_seconds,
            )
            if existing_id:
                return existing_id
        conn.execute(
            """
            INSERT INTO decision_ledger (
                id, created_at, actor, domain, source, observation_kind,
                observation_subject, perceived_state_json, candidate_options_json,
                selected_option, selected_payload_json, confidence, autonomy_level,
                expected_outcome, actual_outcome, outcome_status, review_status,
                action_audit_id, resolved_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_id,
                created_at,
                actor,
                domain,
                source,
                observation_kind,
                observation_subject,
                _json_dumps(perceived_state or {}),
                _json_dumps(candidate_options or []),
                selected_option,
                _json_dumps(selected_payload or {}),
                _float(confidence),
                autonomy_level,
                expected_outcome,
                actual_outcome,
                outcome_status,
                review_status,
                action_audit_id,
                _now() if outcome_status != "pending" else None,
            ),
        )
    return decision_id


def _find_recent_duplicate(
    conn: sqlite3.Connection,
    *,
    source: str,
    observation_kind: str,
    observation_subject: str,
    selected_option: str,
    window_seconds: int,
) -> str | None:
    cutoff = (
        (datetime.now(UTC) - timedelta(seconds=max(1, int(window_seconds))))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    row = conn.execute(
        """
        SELECT id
        FROM decision_ledger
        WHERE source = ?
          AND observation_kind = ?
          AND observation_subject = ?
          AND selected_option = ?
          AND created_at >= ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (source, observation_kind, observation_subject, selected_option, cutoff),
    ).fetchone()
    return str(row["id"]) if row else None


def update_decision_outcome(
    decision_id: str,
    *,
    actual_outcome: str,
    outcome_status: str,
    review_status: str | None = None,
    db_path: Path | str | None = None,
) -> bool:
    updates = {
        "actual_outcome": actual_outcome,
        "outcome_status": outcome_status,
        "resolved_at": _now(),
    }
    with _conn(db_path) as conn:
        if review_status is None:
            cur = conn.execute(
                """
                UPDATE decision_ledger
                SET actual_outcome = ?, outcome_status = ?, resolved_at = ?
                WHERE id = ?
                """,
                [updates["actual_outcome"], updates["outcome_status"], updates["resolved_at"], decision_id],
            )
        else:
            cur = conn.execute(
                """
                UPDATE decision_ledger
                SET actual_outcome = ?, outcome_status = ?, resolved_at = ?, review_status = ?
                WHERE id = ?
                """,
                [
                    updates["actual_outcome"],
                    updates["outcome_status"],
                    updates["resolved_at"],
                    review_status,
                    decision_id,
                ],
            )
    return cur.rowcount > 0


def resolve_task_decisions(
    task_id: str,
    *,
    actual_outcome: str,
    success: bool,
    db_path: Path | str | None = None,
) -> int:
    """Resolve pending decisions that deterministically reference a task id.

    This is intentionally exact-match only. It links the existing task outcome
    learning loop to the decision ledger without fuzzy scans or extra LLM work.
    """
    if not task_id:
        return 0
    outcome_status = "succeeded" if success else "failed"
    review_status = "accepted" if success else "needs_review"
    now = _now()
    quoted_task = f'%"{_escape_like(task_id)}"%'
    with _conn(db_path) as conn:
        candidate_rows = conn.execute(
            """
            SELECT id, observation_subject, perceived_state_json,
                   candidate_options_json, selected_payload_json
            FROM decision_ledger
            WHERE outcome_status = 'pending'
              AND (
                observation_subject = ?
                OR selected_payload_json LIKE ? ESCAPE '\\'
                OR perceived_state_json LIKE ? ESCAPE '\\'
                OR candidate_options_json LIKE ? ESCAPE '\\'
              )
            """,
            (task_id, quoted_task, quoted_task, quoted_task),
        ).fetchall()
        ids = [
            str(row["id"])
            for row in candidate_rows
            if row["observation_subject"] == task_id
            or _json_contains_exact_string(row["selected_payload_json"], task_id)
            or _json_contains_exact_string(row["perceived_state_json"], task_id)
            or _json_contains_exact_string(row["candidate_options_json"], task_id)
        ]
        if not ids:
            return 0
        updated = 0
        for decision_id in ids:
            cur = conn.execute(
                """
            UPDATE decision_ledger
            SET actual_outcome = ?,
                outcome_status = ?,
                review_status = ?,
                resolved_at = ?
            WHERE id = ?
            """,
                (actual_outcome, outcome_status, review_status, now, decision_id),
            )
            updated += int(cur.rowcount or 0)
    return updated


def list_decisions(
    *,
    limit: int = 50,
    outcome_status: str | None = None,
    review_status: str | None = None,
    db_path: Path | str | None = None,
) -> list[dict]:
    params: list[Any] = []
    sql = "SELECT * FROM decision_ledger"
    if outcome_status:
        params.append(outcome_status)
    if review_status:
        params.append(review_status)
    params.append(max(1, min(int(limit or 50), 200)))
    if outcome_status and review_status:
        sql += " WHERE outcome_status = ? AND review_status = ?"
    elif outcome_status:
        sql += " WHERE outcome_status = ?"
    elif review_status:
        sql += " WHERE review_status = ?"
    sql += " ORDER BY created_at DESC LIMIT ?"
    with _conn(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(row) for row in rows]


def decision_feedback_report(
    *,
    hours: int = 168,
    min_failures: int = 2,
    limit: int = 200,
    db_path: Path | str | None = None,
) -> dict:
    """Summarize closed-loop decision outcomes into learning candidates.

    This is the no-extra-cost feedback layer: it does not call an LLM and does
    not mutate policy. It promotes repeated or high-confidence failures into
    reviewable candidates so operators/agents can adjust thresholds, prompts,
    or routing with evidence instead of hard-coded one-off exceptions.
    """
    cutoff = (
        (datetime.now(UTC) - timedelta(hours=max(1, int(hours or 168))))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    capped_limit = max(1, min(int(limit or 200), 1000))
    with _conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM decision_ledger
            WHERE created_at >= ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (cutoff, capped_limit),
        ).fetchall()

    decisions = [_row_to_dict(row) for row in rows]
    groups: dict[tuple[str, str, str, str], dict] = {}
    pending_reviews: list[dict] = []
    for decision in decisions:
        key = (
            str(decision.get("domain") or "general"),
            str(decision.get("source") or ""),
            str(decision.get("observation_kind") or ""),
            str(decision.get("selected_option") or ""),
        )
        group = groups.setdefault(
            key,
            {
                "domain": key[0],
                "source": key[1],
                "observation_kind": key[2],
                "selected_option": key[3],
                "total": 0,
                "failed": 0,
                "succeeded": 0,
                "overridden": 0,
                "pending": 0,
                "confidence_sum": 0.0,
                "high_confidence_failures": 0,
                "sample_decision_ids": [],
                "sample_actual_outcomes": [],
            },
        )
        status = str(decision.get("outcome_status") or "pending")
        confidence = _float(decision.get("confidence"))
        group["total"] += 1
        group["confidence_sum"] += confidence
        if status == "failed":
            group["failed"] += 1
            if confidence >= 0.75:
                group["high_confidence_failures"] += 1
        elif status == "succeeded":
            group["succeeded"] += 1
        elif status == "overridden":
            group["overridden"] += 1
        elif status == "pending":
            group["pending"] += 1
        if _is_review_needed(decision) and len(pending_reviews) < 20:
            pending_reviews.append(_decision_review_sample(decision))
        if status in {"failed", "overridden"}:
            _append_sample(group["sample_decision_ids"], str(decision.get("id") or ""))
            _append_sample(group["sample_actual_outcomes"], str(decision.get("actual_outcome") or ""))

    candidates = [
        _learning_candidate(group, min_failures=max(1, int(min_failures or 2))) for group in groups.values()
    ]
    candidates = [candidate for candidate in candidates if candidate]
    candidates.sort(key=lambda c: (c["severity"], c["failure_rate"], c["failed"]), reverse=True)
    return {
        "window_hours": max(1, int(hours or 168)),
        "sampled_decisions": len(decisions),
        "summary": _feedback_summary(decisions),
        "learning_candidates": candidates,
        "pending_reviews": pending_reviews,
        "contract": {
            "mutates_policy": False,
            "uses_llm": False,
            "recommendation": "review candidates before changing autonomy thresholds or prompt policy",
        },
    }


def create_feedback_review_tasks(
    *,
    hours: int = 168,
    min_failures: int = 2,
    limit: int = 200,
    max_tasks: int = 5,
    db_path: Path | str | None = None,
    task_queue_obj: Any | None = None,
) -> dict:
    """Create review tasks for unresolved decision failure patterns.

    This is intentionally review-first. It does not mutate policy or autonomy
    thresholds; it creates bounded tasks with stable signatures so repeated
    runs do not spawn duplicates.
    """
    report = decision_feedback_report(
        hours=hours,
        min_failures=min_failures,
        limit=limit,
        db_path=db_path,
    )
    tq = task_queue_obj or _default_task_queue()
    if tq is None:
        return {"created": [], "skipped": [], "error": "task_queue_unavailable", "report": report}

    open_signatures = _open_feedback_task_signatures(tq)
    created: list[dict] = []
    skipped: list[dict] = []
    for candidate in report.get("learning_candidates", [])[: max(1, int(max_tasks or 5))]:
        signature = _candidate_signature(candidate)
        if signature in open_signatures:
            skipped.append({"signature": signature, "reason": "open_task_exists"})
            continue
        task = tq.create_task(
            title=_candidate_task_title(candidate),
            description=_candidate_task_description(candidate),
            assigned_agent="codex",
            priority=3,
            confidence=float(candidate.get("confidence_avg") or 0.0),
            confidence_reasoning="decision_feedback_report repeated/failed decision pattern",
            created_by="decision_feedback",
            metadata={
                "domain": "brain",
                "source": "decision_feedback",
                "decision_feedback_signature": signature,
                "pattern": candidate.get("pattern") or {},
                "recommended_actions": candidate.get("recommended_actions") or [],
                "mutates_policy": False,
                "uses_llm": False,
            },
        )
        created.append({"signature": signature, "task_id": task.get("id"), "title": task.get("title")})
        open_signatures.add(signature)
    return {"created": created, "skipped": skipped, "report": report}


def _default_task_queue() -> Any | None:
    try:
        from brain_core.task_queue import task_queue

        return task_queue
    except Exception:
        try:
            from task_queue import task_queue

            return task_queue
        except Exception:
            return None


def _open_feedback_task_signatures(task_queue_obj: Any) -> set[str]:
    signatures: set[str] = set()
    for status in ("pending", "approved", "assigned", "running", "resumed", "paused"):
        try:
            tasks = task_queue_obj.list_tasks(status=status, limit=500)
        except Exception as exc:
            log.debug("decision feedback open-task scan failed for status=%s: %s", status, exc)
            continue
        for task in tasks:
            metadata = task.get("metadata") if isinstance(task, dict) else {}
            if isinstance(metadata, dict):
                signature = metadata.get("decision_feedback_signature")
                if signature:
                    signatures.add(str(signature))
    return signatures


def _candidate_signature(candidate: dict) -> str:
    pattern = candidate.get("pattern") or {}
    raw = json.dumps(pattern, sort_keys=True, ensure_ascii=True, default=str)
    return "decision_feedback:" + uuid.uuid5(uuid.NAMESPACE_URL, raw).hex[:12]


def _candidate_task_title(candidate: dict) -> str:
    pattern = candidate.get("pattern") or {}
    selected = str(pattern.get("selected_option") or "unknown")
    source = str(pattern.get("source") or "unknown")
    return f"Review decision failure pattern: {source}/{selected}"[:200]


def _candidate_task_description(candidate: dict) -> str:
    pattern = candidate.get("pattern") or {}
    actions = ", ".join(candidate.get("recommended_actions") or [])
    samples = ", ".join(candidate.get("sample_decision_ids") or [])
    outcomes = "\n".join(f"- {item}" for item in (candidate.get("sample_actual_outcomes") or [])[:5])
    return (
        "Decision feedback found a repeated or high-confidence failure pattern.\n\n"
        f"Pattern: {json.dumps(pattern, ensure_ascii=False, sort_keys=True)}\n"
        f"Total: {candidate.get('total')} | Failed: {candidate.get('failed')} | "
        f"Failure rate: {candidate.get('failure_rate')} | Severity: {candidate.get('severity')}\n"
        f"Recommended review actions: {actions}\n"
        f"Sample decisions: {samples}\n\n"
        "Sample outcomes:\n"
        f"{outcomes}\n\n"
        "Do not mutate policy automatically. Review the samples, then update policy/thresholds/tests only if evidence supports it."
    )[:5000]


def _feedback_summary(decisions: list[dict]) -> dict:
    statuses: dict[str, int] = {}
    for decision in decisions:
        status = str(decision.get("outcome_status") or "pending")
        statuses[status] = statuses.get(status, 0) + 1
    return {"by_outcome_status": dict(sorted(statuses.items()))}


def _learning_candidate(group: dict, *, min_failures: int) -> dict | None:
    failed = int(group["failed"]) + int(group["overridden"])
    total = int(group["total"])
    if failed < min_failures and int(group["high_confidence_failures"]) == 0:
        return None
    confidence_avg = round(float(group["confidence_sum"]) / max(1, total), 3)
    failure_rate = round(failed / max(1, total), 3)
    actions = _candidate_actions(group, failed=failed, total=total, confidence_avg=confidence_avg)
    return {
        "pattern": {
            "domain": group["domain"],
            "source": group["source"],
            "observation_kind": group["observation_kind"],
            "selected_option": group["selected_option"],
        },
        "total": total,
        "failed": failed,
        "succeeded": int(group["succeeded"]),
        "pending": int(group["pending"]),
        "failure_rate": failure_rate,
        "confidence_avg": confidence_avg,
        "high_confidence_failures": int(group["high_confidence_failures"]),
        "severity": _candidate_severity(failed, failure_rate, int(group["high_confidence_failures"])),
        "recommended_actions": actions,
        "sample_decision_ids": group["sample_decision_ids"],
        "sample_actual_outcomes": group["sample_actual_outcomes"],
    }


def _candidate_actions(group: dict, *, failed: int, total: int, confidence_avg: float) -> list[str]:
    actions = ["inspect_failed_decision_samples"]
    if failed >= 2:
        actions.append("require_review_before_reusing_selected_option")
    if confidence_avg >= 0.75 and failed:
        actions.append("lower_confidence_for_this_pattern_until_reviewed")
    if total >= 3 and failed / max(1, total) >= 0.5:
        actions.append("add_or_update_policy_memory_for_this_pattern")
    if group["source"] == "brain_decide":
        actions.append("tighten_decide_context_or_option_framing")
    if group["source"] == "brain_loop":
        actions.append("tighten_autonomy_gate_or_dispatch_threshold")
    return actions


def _candidate_severity(failed: int, failure_rate: float, high_confidence_failures: int) -> int:
    severity = min(10, failed * 2)
    if failure_rate >= 0.5:
        severity += 2
    if high_confidence_failures:
        severity += 2
    return min(10, severity)


def _is_review_needed(decision: dict) -> bool:
    return str(decision.get("review_status") or "") == "needs_review" or str(
        decision.get("outcome_status") or ""
    ) in {"failed", "overridden"}


def _decision_review_sample(decision: dict) -> dict:
    return {
        "id": decision.get("id"),
        "created_at": decision.get("created_at"),
        "domain": decision.get("domain"),
        "source": decision.get("source"),
        "observation_kind": decision.get("observation_kind"),
        "observation_subject": decision.get("observation_subject"),
        "selected_option": decision.get("selected_option"),
        "confidence": decision.get("confidence"),
        "actual_outcome": decision.get("actual_outcome"),
    }


def _append_sample(items: list[str], value: str, *, limit: int = 5) -> None:
    if value and len(items) < limit:
        items.append(value)


def _row_to_dict(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["perceived_state"] = _json_loads(item.pop("perceived_state_json"), {})
    item["candidate_options"] = _json_loads(item.pop("candidate_options_json"), [])
    item["selected_payload"] = _json_loads(item.pop("selected_payload_json"), {})
    return item


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, default=str)[:20000]


def _json_loads(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def _json_contains_exact_string(value: str | None, target: str) -> bool:
    """Return true only when target is a complete JSON string value.

    This protects task outcome linking from substring collisions such as
    task_1 accidentally resolving a decision for task_10.
    """
    parsed = _json_loads(value, None)
    return _contains_exact_string(parsed, target)


def _contains_exact_string(value: Any, target: str) -> bool:
    if isinstance(value, str):
        return value == target
    if isinstance(value, dict):
        return any(_contains_exact_string(v, target) for v in value.values())
    if isinstance(value, list):
        return any(_contains_exact_string(v, target) for v in value)
    return False


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _float(value: Any) -> float:
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return 0.0


def _now() -> str:
    """Z-suffix UTC timestamp. Delegates to db.now_iso(z_suffix=True)."""
    try:
        from brain_core.db import now_iso
    except ImportError:
        from db import now_iso

    return now_iso(z_suffix=True)
