"""brain_core/goal_subtask_scaffold.py — deterministic brain-self-quality
subtasks from live SLO + feedback signals.

`goal_decompose.decompose_goal` exists but routes through Sage and is gated
behind autonomy L1 — at the default agency level it returns an empty list
and the top brain-improvement goal never picks up any subtasks. Result:
goal progress stays at 0/0 forever and there is no concrete next-best-
action surface for "make the brain better".

This module fills that gap with a **deterministic, LLM-free** scaffold:

  1. Read the current SLO status, decision_feedback candidates, and
     outcome_feedback override patterns.
  2. For each measurable gap (high override domain, low judge volume,
     breached SLO), produce a subtask dict carrying the metric name,
     direction, current value, and target.
  3. Idempotently materialize the missing subtasks under the parent goal:
     each task carries its metric signature in metadata, and re-runs
     skip metrics that already have an open task.

The subtasks are review-first by contract — they describe a concrete
target the brain (or a delegated agent) can pursue, and a deterministic
evaluator (not built here) can later flip them to "completed" when the
metric clears the threshold. No autonomy mutation, no LLM call.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

log = logging.getLogger("brain.goal_subtask_scaffold")


_METRIC_SIGNATURE_KEY = "brain_quality_metric"
# 2026-05-13: brain-quality subtasks now dispatch through cli_llm
# (subscription codex → claude) rather than an OpenClaw persona. The
# label flows through to review_task_dispatcher's eligibility filter.
_DEFAULT_SUBTASK_AGENT = "brain_cli"


# ---------------------------------------------------------------------------
# Metric proposers
# ---------------------------------------------------------------------------


def propose_brain_quality_subtasks(
    *,
    autonomy_db_path: Path | str | None = None,
    brain_db_path: Path | str | None = None,
) -> list[dict]:
    """Build a deterministic list of subtask proposals for a brain-quality goal.

    Each proposal carries:
      - metric_name: unique key for dedupe
      - title / description: human-readable target
      - direction: 'reduce_below' or 'raise_above'
      - target / current: numeric thresholds
      - source: which subsystem produced the proposal
    """
    proposals: list[dict] = []
    proposals.extend(_override_pattern_proposals(autonomy_db_path))
    proposals.extend(_slo_breach_proposals())
    proposals.extend(_judge_volume_proposals(brain_db_path))
    proposals.extend(_uncertainty_proposals(brain_db_path))
    return proposals


def _override_pattern_proposals(autonomy_db_path: Path | str | None) -> list[dict]:
    """One subtask per high-saturation override domain (override_pct >= 70%).

    The threshold matches the autonomy 'frozen' rule: when override_pct
    crosses 95% the domain is frozen and recommendations stop. We surface
    at 70% so the brain can act before its own autonomy gates do.
    """
    try:
        from outcome_feedback import override_patterns_report
    except ImportError:
        log.debug("outcome_feedback unavailable; skipping override proposals")
        return []
    report = override_patterns_report(
        hours=168,
        min_overrides=2,
        limit=1000,
        db_path=autonomy_db_path,
    )
    out: list[dict] = []
    for cand in report.get("learning_candidates") or []:
        rate = float(cand.get("override_rate") or 0.0)
        if rate < 0.7:
            continue
        domain = cand["domain"]
        reason = cand["override_reason"]
        out.append(
            {
                "metric_name": f"override_pct.{domain}",
                "title": (f"Cut override rate in '{domain}' below 50% " f"(currently {int(rate * 100)}%)"),
                "description": _override_subtask_description(cand),
                "direction": "reduce_below",
                "current": round(rate * 100, 1),
                "target": 50.0,
                "unit": "%",
                "source": "outcome_feedback.override_patterns_report",
                "evidence": {
                    "signature": cand["signature"],
                    "override_reason": reason,
                    "sample_task_ids": cand.get("sample_task_ids") or [],
                },
            }
        )
    return out


def _slo_breach_proposals() -> list[dict]:
    """One subtask per breached SLO. Bypasses LLM dispatch — the SLO
    state itself is the metric, the target is "not breached"."""
    try:
        from slos import evaluate_slos
    except ImportError:
        log.debug("slos.evaluate_slos unavailable; skipping breach proposals")
        return []
    try:
        result = evaluate_slos(send_alerts=False)
    except Exception as exc:
        log.debug("evaluate_slos failed: %s", exc)
        return []
    out: list[dict] = []
    for item in result.get("items") or []:
        if not item.get("breached"):
            continue
        name = item.get("name")
        if not name:
            continue
        target = item.get("target")
        actual = item.get("actual")
        unit = item.get("unit") or ""
        out.append(
            {
                "metric_name": f"slo.{name}",
                "title": f"Clear breached SLO '{name}' (target {target}{unit}, actual {actual}{unit})",
                "description": _slo_subtask_description(item),
                "direction": "reduce_below" if _is_upper_bound_slo(item) else "raise_above",
                "current": actual,
                "target": target,
                "unit": unit,
                "source": "slos.evaluate_slos",
                "evidence": {
                    "severity": item.get("severity"),
                    "description": (item.get("description") or "")[:300],
                },
            }
        )
    return out


def _judge_volume_proposals(brain_db_path: Path | str | None) -> list[dict]:
    """recall_judge volume: the user-facing /recall feedback shows ~2% of
    recalls get judged. Aim for >= 5% so wrong-rate has signal."""
    try:
        from config import BRAIN_DB
    except ImportError:
        BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")
    db_path = Path(brain_db_path or BRAIN_DB)
    if not db_path.exists():
        return []
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            recalls = conn.execute(
                "SELECT COUNT(*) FROM action_audit "
                "WHERE route IN ('/recall', '/recall/v2') "
                "  AND created_at > datetime('now', '-7 days')"
            ).fetchone()[0]
            judged = conn.execute(
                "SELECT COUNT(*) FROM action_audit "
                "WHERE route IN ('/recall', '/recall/v2') "
                "  AND created_at > datetime('now', '-7 days') "
                "  AND outcome IN ('judged_good', 'judged_wrong')"
            ).fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.debug("judge volume probe failed: %s", exc)
        return []
    if not recalls:
        return []
    judge_pct = (judged / recalls) * 100
    if judge_pct >= 5.0:
        return []
    return [
        {
            "metric_name": "recall_judge.judged_pct_7d",
            "title": (
                f"Raise /recall judge coverage above 5% "
                f"(currently {round(judge_pct, 2)}% over 7d, {judged}/{recalls})"
            ),
            "description": (
                "recall_judge currently samples too few /recall calls for the "
                "wrong-rate signal to be reliable. Options without extra LLM "
                "spend: bump SAMPLE_SIZE, run twice per day, or add a "
                "deterministic structural judge that runs on every recall "
                "and stores a heuristic score in action_audit.outcome."
            ),
            "direction": "raise_above",
            "current": round(judge_pct, 2),
            "target": 5.0,
            "unit": "%",
            "source": "action_audit.recall_judge_coverage",
            "evidence": {"window_days": 7, "judged": judged, "recalls": recalls},
        }
    ]


def _uncertainty_proposals(brain_db_path: Path | str | None) -> list[dict]:
    """Surface real low-confidence atoms (post conjecture filter) as a
    review subtask if there are any with very low confidence."""
    try:
        from config import BRAIN_DB
    except ImportError:
        BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")
    db_path = Path(brain_db_path or BRAIN_DB)
    if not db_path.exists():
        return []
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM atoms "
                "WHERE tier != 'obsolete' "
                "  AND (kind IS NULL OR kind != 'conjecture') "
                "  AND confidence < 0.4"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    n = int(row[0] or 0) if row else 0
    if n == 0:
        return []
    return [
        {
            "metric_name": "atoms.low_confidence_count",
            "title": f"Resolve or refine {n} low-confidence non-conjecture atoms",
            "description": (
                "These are the genuine uncertainty atoms after dream conjectures "
                "are filtered out. Each one is either a fact that should be "
                "promoted with corroboration, retracted as obsolete, or "
                "explicitly downgraded. Use brain_correct or canonical promotion."
            ),
            "direction": "reduce_below",
            "current": n,
            "target": max(5, n // 4),
            "unit": "atoms",
            "source": "atoms.low_confidence_non_conjecture",
            "evidence": {"confidence_threshold": 0.4, "non_conjecture": True},
        }
    ]


# ---------------------------------------------------------------------------
# Materialization
# ---------------------------------------------------------------------------


def ensure_brain_quality_subtasks(
    *,
    goal_id: str | None = None,
    goal_title_match: tuple[str, ...] = ("brain", "Brain", "뇌"),
    max_create: int = 8,
    task_queue_obj: Any | None = None,
    autonomy_db_path: Path | str | None = None,
    brain_db_path: Path | str | None = None,
) -> dict:
    """Idempotently materialize brain-quality subtasks under the matching goal.

    If `goal_id` is None we pick the highest-priority active goal whose
    title contains one of `goal_title_match`. Each subtask carries its
    metric_name in metadata; existing pending/approved/running subtasks
    with the same metric_name are treated as still satisfying the proposal
    and skipped.
    """
    tq = task_queue_obj or _default_task_queue()
    if tq is None:
        return {"created": [], "skipped": [], "error": "task_queue_unavailable"}

    goal = _resolve_goal(tq, goal_id, goal_title_match)
    if not goal:
        return {"created": [], "skipped": [], "error": "no_matching_goal"}

    proposals = propose_brain_quality_subtasks(
        autonomy_db_path=autonomy_db_path,
        brain_db_path=brain_db_path,
    )
    existing = _existing_metric_names(tq, goal["id"])
    created: list[dict] = []
    skipped: list[dict] = []
    for proposal in proposals[: max(1, int(max_create or 8))]:
        metric = proposal["metric_name"]
        if metric in existing:
            skipped.append({"metric_name": metric, "reason": "open_subtask_exists"})
            continue
        task = tq.create_task(
            title=proposal["title"],
            description=proposal["description"],
            assigned_agent=_DEFAULT_SUBTASK_AGENT,
            priority=3,
            parent_goal_id=goal["id"],
            confidence=0.6,
            confidence_reasoning="goal_subtask_scaffold: deterministic metric-bound proposal",
            created_by="goal_subtask_scaffold",
            metadata={
                "domain": "brain-system",
                "source": "goal_subtask_scaffold",
                _METRIC_SIGNATURE_KEY: metric,
                "direction": proposal["direction"],
                "current": proposal.get("current"),
                "target": proposal.get("target"),
                "unit": proposal.get("unit") or "",
                "metric_source": proposal["source"],
                "evidence": proposal.get("evidence") or {},
                "mutates_policy": False,
                # subtasks themselves are deterministic proposals (no LLM
                # to create them), but the dispatcher will route them
                # through cli_llm for investigation, hence llm_dispatch
                # is annotated for downstream observability.
                "uses_llm": True,
                "llm_dispatch": "cli_llm",
            },
        )
        existing.add(metric)
        created.append({"metric_name": metric, "task_id": task.get("id"), "title": task.get("title")})
    return {
        "goal_id": goal["id"],
        "goal_title": goal.get("title"),
        "created": created,
        "skipped": skipped,
        "proposals_total": len(proposals),
    }


def _resolve_goal(
    task_queue_obj: Any,
    goal_id: str | None,
    title_match: tuple[str, ...],
) -> dict | None:
    if goal_id:
        return task_queue_obj.get_goal(goal_id)
    try:
        goals = task_queue_obj.list_goals(status="active")
    except Exception as exc:
        log.debug("list_goals failed: %s", exc)
        return None
    matches = [
        g
        for g in goals or []
        if isinstance(g, dict) and any(token in str(g.get("title") or "") for token in title_match)
    ]
    if not matches:
        return None
    # Lower priority numbers represent higher priority in the task queue;
    # fall back to created_at recency when priorities tie.
    matches.sort(
        key=lambda g: (
            int(g.get("priority") or 5),
            str(g.get("created_at") or ""),
        )
    )
    return matches[0]


def _existing_metric_names(task_queue_obj: Any, goal_id: str) -> set[str]:
    metric_names: set[str] = set()
    try:
        subtasks = task_queue_obj.list_tasks(parent_goal_id=goal_id) or []
    except Exception:
        return metric_names
    for subtask in subtasks:
        status = str(subtask.get("status") or "")
        if status in {"completed", "cancelled", "failed"}:
            continue
        meta = subtask.get("metadata")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (TypeError, ValueError):
                meta = None
        if isinstance(meta, dict):
            metric = meta.get(_METRIC_SIGNATURE_KEY)
            if metric:
                metric_names.add(str(metric))
    return metric_names


def _default_task_queue() -> Any | None:
    try:
        try:
            from brain_core.task_queue import task_queue
        except ImportError:
            from task_queue import task_queue
        return task_queue
    except Exception as exc:
        log.debug("default task_queue unavailable: %s", exc)
        return None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


_LOWER_IS_BETTER_TOKENS = (
    "fail_rate",
    "_count",
    "_mb",
    "_ms",
    "_pct",
    "stddev",
    "drift",
    "growth",
    "backlog",
    "size",
    "duration",
    "open",
    "missing",
    "breached",
)


def _is_upper_bound_slo(item: dict) -> bool:
    """Heuristic: lower-is-better SLOs name fail rates, sizes, latencies
    or counts; everything else is treated as raise_above."""
    name = str(item.get("name") or "").lower()
    return any(token in name for token in _LOWER_IS_BETTER_TOKENS)


def _override_subtask_description(candidate: dict) -> str:
    actions = candidate.get("recommended_actions") or []
    samples = candidate.get("sample_actions") or []
    lines = [
        "Brain's recommendation in this domain keeps getting overridden.",
        f"Override count: {candidate['overrides']} / {candidate['domain_total']} outcomes",
        f"Domain: {candidate['domain']}",
        f"Override reason: {candidate['override_reason']}",
        "",
        "Sample preferred actions Chris took:",
        *[f"  - {s[:240]}" for s in samples],
        "",
        "Recommended next steps:",
        *[f"  - {a}" for a in actions],
        "",
        "Contract: surface evidence + propose a counterexample, do not mutate autonomy thresholds.",
    ]
    return "\n".join(line for line in lines if line is not None)


def _slo_subtask_description(item: dict) -> str:
    description = (item.get("description") or "").strip() or "(no description recorded)"
    return (
        f"SLO '{item.get('name')}' is breached.\n"
        f"Target: {item.get('target')}{item.get('unit') or ''}\n"
        f"Actual: {item.get('actual')}{item.get('unit') or ''}\n"
        f"Severity: {item.get('severity')}\n\n"
        f"Why this SLO exists:\n{description}\n\n"
        "Goal: investigate the root cause and clear the breach. "
        "Read-only first — confirm the measurement is correct before remediation."
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    prop = sub.add_parser("propose")
    ens = sub.add_parser("ensure")
    ens.add_argument("--goal-id", default=None)
    ens.add_argument("--max", dest="max_create", type=int, default=8)
    args = p.parse_args()
    if args.cmd == "propose":
        print(json.dumps(propose_brain_quality_subtasks(), indent=2, default=str))
    elif args.cmd == "ensure":
        print(
            json.dumps(
                ensure_brain_quality_subtasks(goal_id=args.goal_id, max_create=args.max_create),
                indent=2,
                default=str,
            )
        )
