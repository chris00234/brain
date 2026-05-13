"""Deterministic belief-state snapshot for the brain agency layer.

This module is intentionally read-only and LLM-free. It compiles existing
signals into a transparent state surface; it does not make final decisions or
encode domain-specific exception branches.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

try:
    from brain_core.config import BRAIN_DB
except ImportError:  # pragma: no cover - direct execution fallback
    try:
        from config import BRAIN_DB
    except ImportError:
        BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


BELIEF_STATE_VERSION = 1
DEFAULT_LIMIT = 10
LOW_CONFIDENCE_THRESHOLD = _env_float("BRAIN_BELIEF_LOW_CONFIDENCE_THRESHOLD", 0.4)
STALE_CANONICAL_DAYS = _env_int("BRAIN_BELIEF_STALE_CANONICAL_DAYS", 180)


@contextmanager
def _read_conn(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    path = Path(db_path or BRAIN_DB)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def build_belief_state(
    *,
    brain_db: Path | str | None = None,
    task_queue_obj: Any | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict:
    """Build a deterministic snapshot of beliefs, uncertainty, goals, outcomes.

    The snapshot favors abstention over overconfidence: missing tables or
    unavailable stores return empty sections plus warnings instead of creating
    synthetic conclusions.
    """

    safe_limit = max(1, min(int(limit or DEFAULT_LIMIT), 50))
    warnings: list[dict] = []
    generated_at = _now()

    beliefs: list[dict] = []
    uncertainties: list[dict] = []
    try:
        with _read_conn(brain_db) as conn:
            beliefs = _load_beliefs(conn, safe_limit)
            uncertainties = _load_uncertainties(conn, safe_limit)
    except sqlite3.Error as exc:
        warnings.append({"source": "atoms", "reason": "unavailable", "detail": str(exc)[:160]})

    tq = task_queue_obj or _default_task_queue(warnings)
    goals = _load_goals(tq, safe_limit, warnings) if tq is not None else []
    recent_outcomes = _load_recent_outcomes(tq, safe_limit, warnings) if tq is not None else []
    decision_feedback = _load_decision_feedback(tq, safe_limit, warnings) if tq is not None else {}
    override_patterns = _load_override_patterns(tq, safe_limit, warnings) if tq is not None else {}
    trend_alerts = _load_trend_alerts(warnings)
    next_actions = _derive_next_actions(
        goals=goals,
        uncertainties=uncertainties,
        outcomes=recent_outcomes,
        decision_feedback=decision_feedback,
        override_patterns=override_patterns,
        trend_alerts=trend_alerts,
        limit=min(5, safe_limit),
    )
    world_model = _derive_world_model(
        goals=goals,
        uncertainties=uncertainties,
        outcomes=recent_outcomes,
        decision_feedback=decision_feedback,
        override_patterns=override_patterns,
        trend_alerts=trend_alerts,
        next_actions=next_actions,
        autonomy_db_path=str(getattr(tq, "_db_path", None)) if tq is not None else None,
    )

    return {
        "version": BELIEF_STATE_VERSION,
        "generated_at": generated_at,
        "policy": {
            "mode": "deterministic_read_only",
            "llm": "none",
            "low_confidence_threshold": LOW_CONFIDENCE_THRESHOLD,
            "stale_canonical_days": STALE_CANONICAL_DAYS,
        },
        "summary": {
            "beliefs": len(beliefs),
            "uncertainties": len(uncertainties),
            "active_goals": len(goals),
            "recent_outcomes": len(recent_outcomes),
            "decision_feedback_candidates": len(decision_feedback.get("learning_candidates") or []),
            "override_patterns": len(override_patterns.get("learning_candidates") or []),
            "trend_alerts": len(trend_alerts),
            "warnings": len(warnings),
        },
        "world_model": world_model,
        "operating_constraints": _operating_constraints(),
        "beliefs": beliefs,
        "uncertainties": uncertainties,
        "goals": goals,
        "recent_outcomes": recent_outcomes,
        "decision_feedback": decision_feedback,
        "override_patterns": override_patterns,
        "trend_alerts": trend_alerts,
        "next_actions": next_actions,
        "warnings": warnings,
    }


def _load_beliefs(conn: sqlite3.Connection, limit: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, text, kind, tier, canonical, confidence, trust_score,
               quality_score, valid_until, provenance_json, updated_at
        FROM atoms
        WHERE tier != 'obsolete'
          AND provisional = 0
          AND confidence >= ?
        ORDER BY canonical DESC,
                 confidence DESC,
                 trust_score DESC,
                 updated_at DESC
        LIMIT ?
        """,
        (LOW_CONFIDENCE_THRESHOLD, limit),
    ).fetchall()
    return [_belief_from_row(row) for row in rows]


def _load_uncertainties(conn: sqlite3.Connection, limit: int) -> list[dict]:
    # Exclude kind='conjecture': dream_replay emits low-confidence hypothesis
    # atoms by design (CONJECTURE_CONFIDENCE=0.3 < LOW_CONFIDENCE_THRESHOLD=0.4),
    # so they would otherwise dominate the uncertainty surface and drown out
    # real low-confidence beliefs and stale canonicals that need review.
    uncertainties: list[dict] = []
    low_confidence = conn.execute(
        """
        SELECT id, text, kind, tier, canonical, confidence, trust_score,
               quality_score, valid_until, provenance_json, updated_at
        FROM atoms
        WHERE tier != 'obsolete'
          AND (kind IS NULL OR kind != 'conjecture')
          AND confidence < ?
        ORDER BY confidence ASC, updated_at DESC
        LIMIT ?
        """,
        (LOW_CONFIDENCE_THRESHOLD, limit),
    ).fetchall()
    for row in low_confidence:
        item = _belief_from_row(row)
        item["reason"] = "low_confidence"
        item["needs_review"] = True
        uncertainties.append(item)

    remaining = max(0, limit - len(uncertainties))
    if remaining:
        stale_cutoff = (
            (datetime.now(UTC) - timedelta(days=STALE_CANONICAL_DAYS))
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        stale_rows = conn.execute(
            """
            SELECT id, text, kind, tier, canonical, confidence, trust_score,
                   quality_score, valid_until, provenance_json, updated_at
            FROM atoms
            WHERE tier != 'obsolete'
              AND canonical = 1
              AND updated_at < ?
            ORDER BY updated_at ASC
            LIMIT ?
            """,
            (stale_cutoff, remaining),
        ).fetchall()
        for row in stale_rows:
            item = _belief_from_row(row)
            item["reason"] = "stale_canonical"
            item["needs_review"] = True
            uncertainties.append(item)

    return uncertainties


def _belief_from_row(row: sqlite3.Row) -> dict:
    confidence = _float(row["confidence"])
    trust_score = _float(row["trust_score"])
    quality_score = _float(row["quality_score"])
    freshness = _freshness(row["updated_at"], row["valid_until"])
    return {
        "id": row["id"],
        "text": (row["text"] or "")[:500],
        "kind": row["kind"],
        "tier": row["tier"],
        "canonical": bool(row["canonical"]),
        "confidence": confidence,
        "trust_score": trust_score,
        "quality_score": quality_score,
        "updated_at": row["updated_at"],
        "valid_until": row["valid_until"],
        "freshness": freshness,
        "support_score": _support_score(confidence, trust_score, quality_score, freshness),
        "needs_review": freshness != "fresh" or confidence < LOW_CONFIDENCE_THRESHOLD,
    }


def _load_goals(task_queue_obj: Any, limit: int, warnings: list[dict]) -> list[dict]:
    try:
        raw_goals = task_queue_obj.list_goals(status="active")[:limit]
    except Exception as exc:
        warnings.append({"source": "goals", "reason": "unavailable", "detail": str(exc)[:160]})
        return []

    goals: list[dict] = []
    for goal in raw_goals:
        progress = _goal_progress(task_queue_obj, goal.get("id"), warnings)
        metadata = goal.get("metadata") if isinstance(goal.get("metadata"), dict) else {}
        priority_score, reasons = _goal_priority_score(goal, progress, metadata)
        goals.append(
            {
                "id": goal.get("id"),
                "title": goal.get("title"),
                "description": (goal.get("description") or "")[:500],
                "status": goal.get("status"),
                "created_by": goal.get("created_by"),
                "created_at": goal.get("created_at"),
                "updated_at": goal.get("updated_at"),
                "progress": progress,
                "priority_score": priority_score,
                "priority_reasons": reasons,
            }
        )
    goals.sort(key=lambda item: item["priority_score"], reverse=True)
    return goals


def _goal_progress(task_queue_obj: Any, goal_id: str | None, warnings: list[dict]) -> dict:
    if not goal_id:
        return {}
    try:
        progress = task_queue_obj.get_goal_progress(goal_id)
        return progress if isinstance(progress, dict) else {}
    except Exception as exc:
        warnings.append({"source": "goal_progress", "reason": "unavailable", "detail": str(exc)[:160]})
        return {}


def _load_recent_outcomes(task_queue_obj: Any, limit: int, warnings: list[dict]) -> list[dict]:
    try:
        outcomes = task_queue_obj.list_outcomes(limit=limit)
    except Exception as exc:
        warnings.append({"source": "outcomes", "reason": "unavailable", "detail": str(exc)[:160]})
        return []
    return [
        {
            "id": row.get("id"),
            "task_id": row.get("task_id"),
            "domain": row.get("domain"),
            "chris_override": bool(row.get("chris_override")),
            "override_reason": row.get("override_reason") or "",
            "confidence_was": _float(row.get("confidence_was")),
            "created_at": row.get("created_at"),
        }
        for row in outcomes[:limit]
    ]


def _load_decision_feedback(task_queue_obj: Any, limit: int, warnings: list[dict]) -> dict:
    try:
        from brain_core.decision_ledger import decision_feedback_report
    except ImportError:
        try:
            from decision_ledger import decision_feedback_report
        except Exception as exc:
            warnings.append(
                {"source": "decision_feedback", "reason": "unavailable", "detail": str(exc)[:160]}
            )
            return {}
    try:
        db_path = getattr(task_queue_obj, "_db_path", None)
        return decision_feedback_report(hours=168, min_failures=1, limit=max(20, limit * 4), db_path=db_path)
    except Exception as exc:
        warnings.append({"source": "decision_feedback", "reason": "unavailable", "detail": str(exc)[:160]})
        return {}


def _load_trend_alerts(warnings: list[dict]) -> list[dict]:
    """Drift alerts from metric_trend_tracker — surfaces "things got worse over 7d"."""
    try:
        from brain_core.metric_trend_tracker import compute_trend_alerts
    except ImportError:
        try:
            from metric_trend_tracker import compute_trend_alerts
        except Exception as exc:
            warnings.append({"source": "metric_trend", "reason": "unavailable", "detail": str(exc)[:160]})
            return []
    try:
        return compute_trend_alerts()
    except Exception as exc:
        warnings.append({"source": "metric_trend", "reason": "unavailable", "detail": str(exc)[:160]})
        return []


def _load_override_patterns(task_queue_obj: Any, limit: int, warnings: list[dict]) -> dict:
    """Override patterns from the outcomes table — the missing twin of
    decision_feedback. decision_ledger only sees brain_loop's own decisions;
    chris_override traffic on outcomes (250+ rows/30d in infra) goes
    nowhere unless this report fans it back into the belief state."""
    try:
        from brain_core.outcome_feedback import override_patterns_report
    except ImportError:
        try:
            from outcome_feedback import override_patterns_report
        except Exception as exc:
            warnings.append({"source": "outcome_feedback", "reason": "unavailable", "detail": str(exc)[:160]})
            return {}
    try:
        db_path = getattr(task_queue_obj, "_db_path", None)
        return override_patterns_report(
            hours=168,
            min_overrides=2,
            limit=max(200, limit * 20),
            db_path=db_path,
        )
    except Exception as exc:
        warnings.append({"source": "outcome_feedback", "reason": "unavailable", "detail": str(exc)[:160]})
        return {}


def _derive_next_actions(
    *,
    goals: list[dict],
    uncertainties: list[dict],
    outcomes: list[dict],
    decision_feedback: dict,
    override_patterns: dict | None = None,
    trend_alerts: list[dict] | None = None,
    limit: int,
) -> list[dict]:
    actions: list[dict] = []
    candidates = decision_feedback.get("learning_candidates") or []
    if candidates:
        top = candidates[0]
        actions.append(
            {
                "type": "review_decision_feedback",
                "reason": "failed_or_overridden_decision_pattern",
                "target": top.get("pattern"),
                "priority": round(min(1.0, 0.75 + float(top.get("severity") or 0) / 20), 3),
            }
        )
    override_candidates = (override_patterns or {}).get("learning_candidates") or []
    if override_candidates:
        top_ov = override_candidates[0]
        actions.append(
            {
                "type": "review_override_pattern",
                "reason": "chris_override_pattern_detected",
                "target": {
                    "signature": top_ov.get("signature"),
                    "domain": top_ov.get("domain"),
                    "overrides": top_ov.get("overrides"),
                    "override_rate": top_ov.get("override_rate"),
                },
                "priority": round(min(1.0, 0.7 + float(top_ov.get("severity") or 0) * 0.25), 3),
            }
        )
    for alert in (trend_alerts or [])[:2]:
        actions.append(
            {
                "type": "review_metric_drift",
                "reason": f"7d_drift_on_{alert.get('metric')}",
                "target": {
                    "metric": alert.get("metric"),
                    "label": alert.get("label"),
                    "current": alert.get("current"),
                    "baseline": alert.get("baseline"),
                    "delta": alert.get("delta"),
                },
                "priority": 0.85,
            }
        )
    override_count = sum(1 for outcome in outcomes if outcome.get("chris_override"))
    if override_count:
        actions.append(
            {
                "type": "review_decision_quality",
                "reason": "recent_overrides_present",
                "priority": round(min(1.0, 0.5 + override_count / max(len(outcomes), 1)), 3),
            }
        )
    if uncertainties:
        top = uncertainties[0]
        actions.append(
            {
                "type": "review_uncertainty",
                "target_id": top.get("id"),
                "reason": top.get("reason", "needs_review"),
                "priority": 0.8,
            }
        )
    if goals:
        top_goal = goals[0]
        actions.append(
            {
                "type": "advance_goal",
                "target_id": top_goal.get("id"),
                "reason": "highest_current_priority_score",
                "priority": top_goal.get("priority_score", 0.0),
            }
        )
    if not actions:
        actions.append({"type": "observe", "reason": "no_active_pressure", "priority": 0.1})
    return sorted(actions, key=lambda item: item["priority"], reverse=True)[:limit]


_AGENCY_MIN_SAMPLES = 50
_AGENCY_OVERRIDE_PCT_GRADUATE = 5.0
_AGENCY_OVERRIDE_PCT_FROZEN = 15.0


def _compute_per_domain_agency(autonomy_db_path: str | None) -> dict:
    """Compute per-domain agency level from chris_override rates over 30d.

    2026-05-12: closes D5 — agency was globally hardcoded to
    'review_first_closed_loop'. With 1198 outcomes in 'general' at 0.3%
    override rate, that domain has earned graduation. Other low-volume or
    high-override domains stay locked. Per-domain levels let well-behaved
    surfaces act more autonomously while keeping risky ones gated.

    Returns: {"domains": {domain: {level, override_pct, total}}, "overall": <level>}
    The 'overall' is the most-cautious level across active domains so the
    legacy top-level agency_level still represents the floor of trust.
    """
    out = {"domains": {}, "overall": "review_first_closed_loop"}
    if not autonomy_db_path:
        return out
    try:
        conn = sqlite3.connect(autonomy_db_path, timeout=3)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT domain, COUNT(*) as total, "
                "       SUM(CASE WHEN chris_override = 1 THEN 1 ELSE 0 END) as overrides "
                "FROM outcomes "
                "WHERE created_at > datetime('now', '-30 days') "
                "  AND domain IS NOT NULL "
                "GROUP BY domain"
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return out

    levels_seen: list[str] = []
    for r in rows:
        domain = r["domain"] or "general"
        total = int(r["total"] or 0)
        overrides = int(r["overrides"] or 0)
        override_pct = (100.0 * overrides / total) if total else 0.0
        if total < _AGENCY_MIN_SAMPLES:
            level = "review_first_closed_loop"
        elif override_pct > _AGENCY_OVERRIDE_PCT_FROZEN:
            level = "frozen"
        elif override_pct < _AGENCY_OVERRIDE_PCT_GRADUATE:
            level = "propose_and_inform"
        else:
            level = "review_first_closed_loop"
        out["domains"][domain] = {
            "level": level,
            "override_pct": round(override_pct, 2),
            "total": total,
            "overrides": overrides,
        }
        levels_seen.append(level)
    if levels_seen:
        rank = {"frozen": 0, "review_first_closed_loop": 1, "propose_and_inform": 2}
        out["overall"] = min(levels_seen, key=lambda lvl: rank.get(lvl, 1))
    return out


def _derive_world_model(
    *,
    goals: list[dict],
    uncertainties: list[dict],
    outcomes: list[dict],
    decision_feedback: dict,
    next_actions: list[dict],
    override_patterns: dict | None = None,
    trend_alerts: list[dict] | None = None,
    autonomy_db_path: str | None = None,
) -> dict:
    top_goal = goals[0] if goals else None
    candidates = decision_feedback.get("learning_candidates") or []
    override_candidates = (override_patterns or {}).get("learning_candidates") or []
    agency = _compute_per_domain_agency(autonomy_db_path)
    return {
        "version": 1,
        "top_goal": _compact_goal(top_goal),
        "open_loops": {
            "uncertainties": len(uncertainties),
            "decision_feedback_candidates": len(candidates),
            "override_patterns": len(override_candidates),
            "trend_alerts": len(trend_alerts or []),
            "recent_overrides": sum(1 for outcome in outcomes if outcome.get("chris_override")),
        },
        "highest_risk": _highest_risk(uncertainties, candidates, override_candidates),
        "next_best_action": next_actions[0] if next_actions else {"type": "observe", "priority": 0.1},
        "agency_level": agency["overall"],
        "per_domain_agency": agency["domains"],
        "trend_alerts": trend_alerts or [],
    }


def _compact_goal(goal: dict | None) -> dict | None:
    if not goal:
        return None
    return {
        "id": goal.get("id"),
        "title": goal.get("title"),
        "priority_score": goal.get("priority_score"),
        "progress": goal.get("progress") or {},
    }


def _highest_risk(
    uncertainties: list[dict],
    candidates: list[dict],
    override_candidates: list[dict] | None = None,
) -> dict:
    # Repeated chris_override patterns are a stronger correctness signal
    # than a single low-confidence atom: brain mis-recommended N times in
    # the same shape. They take precedence over single-atom uncertainties
    # but defer to decision_feedback candidates (those already carry a
    # severity score from a closed-loop decision outcome).
    if candidates:
        top = candidates[0]
        return {
            "type": "decision_feedback",
            "severity": top.get("severity"),
            "pattern": top.get("pattern"),
        }
    if override_candidates:
        top_ov = override_candidates[0]
        return {
            "type": "override_pattern",
            "severity": top_ov.get("severity"),
            "signature": top_ov.get("signature"),
            "domain": top_ov.get("domain"),
            "override_reason": top_ov.get("override_reason"),
        }
    if uncertainties:
        return {
            "type": "uncertainty",
            "target_id": uncertainties[0].get("id"),
            "reason": uncertainties[0].get("reason"),
        }
    return {"type": "none"}


def _operating_constraints() -> list[dict]:
    return [
        {
            "id": "no_extra_llm_api_cost",
            "rule": "Use existing Claude/GPT subscription paths for LLM generation; avoid extra paid API billing.",
        },
        {
            "id": "resource_preservation",
            "rule": "Prefer bounded on-demand work over standing daemons or unbounded background fanout.",
        },
        {
            "id": "local_generation_excluded",
            "rule": "Local models are allowed for embeddings/light ranking only, not local LLM generation.",
        },
        {
            "id": "review_before_policy_mutation",
            "rule": "Decision feedback may create review tasks, but policy changes require evidence review.",
        },
    ]


def _goal_priority_score(goal: dict, progress: dict, metadata: dict) -> tuple[float, list[str]]:
    reasons: list[str] = []
    score = 0.35

    age_days = _age_days(goal.get("updated_at") or goal.get("created_at"))
    if age_days is not None:
        score += min(0.2, age_days / 30 * 0.2)
        reasons.append("older_active_goal")

    total = int(progress.get("total") or 0)
    running = int(progress.get("running") or 0)
    failed = int(progress.get("failed") or 0)
    pending = int(progress.get("pending") or 0)
    if total == 0:
        score += 0.15
        reasons.append("not_decomposed")
    if running or pending:
        score += min(0.2, (running + pending) / max(total, 1) * 0.2)
        reasons.append("has_open_work")
    if failed:
        score += min(0.15, failed / max(total, 1) * 0.15)
        reasons.append("has_failed_work")

    priority = _float(metadata.get("priority"))
    if priority > 0:
        normalized = priority / 10 if priority > 1 else priority
        score += min(0.2, max(0.0, normalized) * 0.2)
        reasons.append("metadata_priority")

    return round(min(1.0, max(0.0, score)), 3), reasons or ["active_goal"]


def _support_score(
    confidence: float,
    trust_score: float,
    quality_score: float,
    freshness: str,
) -> float:
    score = confidence * 0.5 + trust_score * 0.35 + quality_score * 0.15
    if freshness == "expired":
        score *= 0.4
    elif freshness == "stale":
        score *= 0.7
    return round(min(1.0, max(0.0, score)), 3)


def _freshness(updated_at: str | None, valid_until: str | None) -> str:
    now = datetime.now(UTC)
    valid_until_dt = _parse_dt(valid_until)
    if valid_until_dt and valid_until_dt < now:
        return "expired"
    updated = _parse_dt(updated_at)
    if updated is None:
        return "unknown"
    if (now - updated).days >= STALE_CANONICAL_DAYS:
        return "stale"
    return "fresh"


def _age_days(value: str | None) -> int | None:
    parsed = _parse_dt(value)
    if parsed is None:
        return None
    return max(0, (datetime.now(UTC) - parsed).days)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _float(value: Any) -> float:
    try:
        if value is None:
            return 0.0
        return round(float(value), 3)
    except (TypeError, ValueError):
        return 0.0


def _default_task_queue(warnings: list[dict]) -> Any | None:
    try:
        from brain_core.task_queue import task_queue
    except ImportError:
        try:
            from task_queue import task_queue
        except Exception as exc:
            warnings.append({"source": "task_queue", "reason": "unavailable", "detail": str(exc)[:160]})
            return None
    except Exception as exc:
        warnings.append({"source": "task_queue", "reason": "unavailable", "detail": str(exc)[:160]})
        return None
    return task_queue


def _now() -> str:
    """Z-suffix UTC timestamp. Delegates to db.now_iso(z_suffix=True)."""
    try:
        from brain_core.db import now_iso
    except ImportError:
        from db import now_iso

    return now_iso(z_suffix=True)
