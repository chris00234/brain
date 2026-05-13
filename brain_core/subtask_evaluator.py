"""brain_core/subtask_evaluator.py — auto-evaluate brain-quality subtask metrics.

`goal_subtask_scaffold` creates subtasks with a target metric (e.g.
"reduce override_pct.coding below 50%"). Without an evaluator the
subtasks sit pending forever even when the metric clears, so the parent
brain-quality goal never reflects real progress.

This module reads each open subtask carrying a `brain_quality_metric`
key in metadata, re-computes the metric's current value, and:

  * auto-completes the task when the metric crosses its target,
  * refreshes `metadata.current` + `metadata.last_evaluated_at` so the
    task description stays trustworthy when displayed in the dashboard
    or via /brain/state.

No autonomy mutation, no LLM call. The evaluator deliberately mirrors
the same metric sources that `goal_subtask_scaffold` used to propose
the subtask: override_pct from outcome_feedback, SLO state from slos,
recall judge coverage from action_audit, low-confidence atom count
from atoms.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

log = logging.getLogger("brain.subtask_evaluator")

_METRIC_KEY = "brain_quality_metric"


def evaluate_brain_quality_subtasks(
    *,
    autonomy_db_path: Path | str | None = None,
    brain_db_path: Path | str | None = None,
    task_queue_obj: Any | None = None,
) -> dict:
    """Walk open brain-quality subtasks, mark cleared ones complete, refresh the rest."""
    tq = task_queue_obj or _default_task_queue()
    if tq is None:
        return {"completed": [], "refreshed": [], "skipped": [], "error": "task_queue_unavailable"}

    snapshot = _metric_snapshot(autonomy_db_path, brain_db_path)
    subtasks = _open_brain_quality_subtasks(tq)

    completed: list[dict] = []
    refreshed: list[dict] = []
    skipped: list[dict] = []
    for subtask in subtasks:
        meta = subtask.get("metadata") or {}
        metric = str(meta.get(_METRIC_KEY) or "")
        if not metric:
            continue
        target = _to_float(meta.get("target"))
        direction = str(meta.get("direction") or "reduce_below")
        current = snapshot.get(metric)
        if current is None:
            skipped.append({"task_id": subtask["id"], "metric_name": metric, "reason": "metric_unavailable"})
            continue
        cleared = _metric_cleared(direction, current, target)
        if cleared:
            try:
                tq.auto_complete_task(
                    subtask["id"],
                    result=f"metric '{metric}' cleared target {target} (current {current})",
                    by="subtask_evaluator",
                )
                completed.append(
                    {
                        "task_id": subtask["id"],
                        "metric_name": metric,
                        "target": target,
                        "achieved": current,
                    }
                )
            except Exception as exc:
                log.warning("auto_complete_task failed for %s: %s", subtask["id"], exc)
                skipped.append(
                    {
                        "task_id": subtask["id"],
                        "metric_name": metric,
                        "reason": f"complete_failed:{str(exc)[:80]}",
                    }
                )
            continue
        try:
            tq._merge_task_metadata(
                subtask["id"],
                {
                    "current": current,
                    "last_evaluated_at": _now_iso(),
                    "last_evaluated_by": "subtask_evaluator",
                },
            )
            refreshed.append({"task_id": subtask["id"], "metric_name": metric, "current": current})
        except Exception as exc:
            log.debug("metadata refresh failed for %s: %s", subtask["id"], exc)
    return {
        "completed": completed,
        "refreshed": refreshed,
        "skipped": skipped,
        "metric_snapshot": snapshot,
    }


# ---------------------------------------------------------------------------
# metric sources
# ---------------------------------------------------------------------------


def _metric_snapshot(
    autonomy_db_path: Path | str | None,
    brain_db_path: Path | str | None,
) -> dict[str, float]:
    snap: dict[str, float] = {}
    snap.update(_override_pct_snapshot(autonomy_db_path))
    snap.update(_slo_snapshot())
    snap.update(_judge_coverage_snapshot(brain_db_path))
    snap.update(_low_confidence_snapshot(brain_db_path))
    return snap


def _override_pct_snapshot(autonomy_db_path: Path | str | None) -> dict[str, float]:
    try:
        from outcome_feedback import override_patterns_report
    except ImportError:
        return {}
    try:
        report = override_patterns_report(hours=168, min_overrides=1, limit=2000, db_path=autonomy_db_path)
    except Exception as exc:
        log.debug("override_patterns_report failed: %s", exc)
        return {}
    # Use the SAME aggregation that goal_subtask_scaffold uses so the
    # evaluator and the proposer agree on the metric value. We aggregate
    # by domain across all override_reasons within the window.
    by_domain: dict[str, tuple[int, int]] = {}
    domains = report.get("domains") or {}
    for cand in report.get("learning_candidates") or []:
        domain = cand["domain"]
        overrides_so_far, _domain_total_seen = by_domain.get(domain, (0, 0))
        by_domain[domain] = (
            overrides_so_far + int(cand.get("overrides", 0)),
            int(domains.get(domain, 0) or 0),
        )
    snap: dict[str, float] = {}
    for domain, (overrides, domain_total) in by_domain.items():
        if domain_total <= 0:
            continue
        snap[f"override_pct.{domain}"] = round((overrides / domain_total) * 100, 2)
    return snap


def _slo_snapshot() -> dict[str, float]:
    try:
        from slos import evaluate_slos
    except ImportError:
        return {}
    try:
        result = evaluate_slos(send_alerts=False)
    except Exception as exc:
        log.debug("evaluate_slos failed: %s", exc)
        return {}
    snap: dict[str, float] = {}
    for item in result.get("items") or []:
        name = item.get("name")
        if not name:
            continue
        snap[f"slo.{name}"] = _to_float(item.get("actual"))
    return snap


def _judge_coverage_snapshot(brain_db_path: Path | str | None) -> dict[str, float]:
    try:
        from config import BRAIN_DB
    except ImportError:
        BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")
    db_path = Path(brain_db_path or BRAIN_DB)
    if not db_path.exists():
        return {}
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
                "  AND outcome IN ('judged_good', 'judged_wrong', "
                "                 'structural_good', 'structural_wrong')"
            ).fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    if not recalls:
        return {}
    return {"recall_judge.judged_pct_7d": round((judged / recalls) * 100, 2)}


def _low_confidence_snapshot(brain_db_path: Path | str | None) -> dict[str, float]:
    try:
        from config import BRAIN_DB
    except ImportError:
        BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")
    db_path = Path(brain_db_path or BRAIN_DB)
    if not db_path.exists():
        return {}
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
        return {}
    return {"atoms.low_confidence_count": float(int(row[0] or 0)) if row else 0.0}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _open_brain_quality_subtasks(task_queue_obj: Any) -> list[dict]:
    open_tasks: list[dict] = []
    seen: set[str] = set()
    for status in ("pending", "approved", "assigned", "running", "resumed", "paused"):
        try:
            tasks = task_queue_obj.list_tasks(status=status) or []
        except Exception as exc:
            log.debug("list_tasks(status=%s) failed: %s", status, exc)
            continue
        for task in tasks:
            if not isinstance(task, dict):
                continue
            tid = task.get("id")
            if not tid or tid in seen:
                continue
            meta = task.get("metadata") or {}
            if isinstance(meta, dict) and meta.get(_METRIC_KEY):
                seen.add(tid)
                open_tasks.append(task)
    return open_tasks


def _metric_cleared(direction: str, current: float, target: float | None) -> bool:
    if target is None:
        return False
    if direction == "reduce_below":
        return current <= target
    if direction == "raise_above":
        return current >= target
    return False


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="seconds")


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


if __name__ == "__main__":
    import argparse
    import json

    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if args.dry_run:
        # Show the snapshot only — no DB writes.
        snap = _metric_snapshot(None, None)
        print(json.dumps({"metric_snapshot": snap}, indent=2, default=str))
    else:
        print(json.dumps(evaluate_brain_quality_subtasks(), indent=2, default=str))
