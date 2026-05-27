"""brain_core/slo_replan.py — SLO-driven self-replanning.

Reads `logs/slo_remediation.jsonl` for the last 7 days. When the same
SLO has triggered remediation ≥ N times and the breach has NOT cleared,
queue a single review task asking brain_cli to propose a structural fix.

Contract:
  - Read-only against slo_remediation.jsonl
  - Writes at most one review task per SLO per run (dedupe by signature)
  - No policy mutation; never opens a PR — that's the operator's call
  - LLM dispatch happens at task execution, not here
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger("brain.slo_replan")

DEFAULT_LOG = Path("/Users/chrischo/server/brain/logs/slo_remediation.jsonl")
DEFAULT_WINDOW_DAYS = 7
DEFAULT_MIN_TRIGGERS = 3
MAX_TASKS_PER_RUN = 3


def _signature(slo_name: str) -> str:
    return f"slo_replan::{slo_name}"


def _load_triggers(
    log_path: Path | str,
    window_days: int,
) -> list[dict]:
    """Read the trigger entries in the JSONL log within the window."""
    path = Path(log_path)
    if not path.exists():
        return []
    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    out: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("kind") != "trigger":
                    continue
                ts_raw = rec.get("timestamp", "")
                try:
                    ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                if ts < cutoff:
                    continue
                out.append(rec)
    except OSError as exc:
        log.debug("slo_replan: could not read %s: %s", path, exc)
        return []
    return out


def _currently_breached_set() -> set[str] | None:
    """Return names of SLOs that are breaching RIGHT NOW.

    Returns None when slos.evaluate_slos is unavailable so the caller can
    fall back to the legacy (count-only) behavior; returns the live-breach
    name set otherwise. Used to prevent slo_replan from queueing a
    structural-fix task for an SLO whose breach has already cleared.
    """
    try:
        from slos import evaluate_slos
    except ImportError:
        return None
    try:
        result = evaluate_slos(send_alerts=False)
    except Exception:
        return None
    return {
        item.get("name") for item in result.get("items") or [] if item.get("breached") and item.get("name")
    }


def find_repeat_breaches(
    *,
    log_path: Path | str = DEFAULT_LOG,
    window_days: int = DEFAULT_WINDOW_DAYS,
    min_triggers: int = DEFAULT_MIN_TRIGGERS,
    require_currently_breached: bool = True,
) -> list[dict]:
    """Return SLOs with ≥ min_triggers remediation triggers in the window.

    2026-05-19: ``require_currently_breached`` (default True) filters out
    SLOs whose breach has already cleared. Historical trigger count alone
    was queueing tasks for healthy metrics (e.g. logs_dir_growth_24h_mb
    fired 267× during a one-time WAL leak but has been quiet since the
    fix landed), stalling the top brain-quality goal on already-resolved
    work. Pass False only when you specifically want historical replay.
    """
    triggers = _load_triggers(log_path, window_days)
    counts: Counter[str] = Counter()
    last_seen: dict[str, dict] = {}
    for rec in triggers:
        slo = rec.get("slo")
        if not slo:
            continue
        counts[slo] += 1
        last_seen[slo] = rec
    live_breaches = _currently_breached_set() if require_currently_breached else None
    out: list[dict] = []
    for slo, n in counts.most_common():
        if n < min_triggers:
            continue
        if live_breaches is not None and slo not in live_breaches:
            continue
        rec = last_seen[slo]
        out.append(
            {
                "slo": slo,
                "triggers": n,
                "window_days": window_days,
                "last_action": rec.get("action"),
                "last_reason": rec.get("reason"),
                "last_current": rec.get("current"),
                "last_threshold": rec.get("threshold"),
                "last_status": rec.get("status"),
                "last_timestamp": rec.get("timestamp"),
            }
        )
    return out


def _task_title(breach: dict) -> str:
    return f"Propose structural fix for SLO {breach['slo']} (triggered {breach['triggers']}x)"


def _task_description(breach: dict) -> str:
    return (
        f"SLO {breach['slo']} triggered remediation {breach['triggers']}x in the last "
        f"{breach['window_days']} days. The last remediation action was "
        f"`{breach['last_action']}` with status `{breach['last_status']}`. "
        f"Current value: {breach['last_current']}, threshold: {breach['last_threshold']}.\n\n"
        f"Reason given: {breach['last_reason']}\n\n"
        "Investigate the underlying breach, propose a structural fix, and open a PR. "
        "Do NOT close the loop automatically — the operator approves before merge."
    )


def materialize_review_tasks(
    *,
    log_path: Path | str = DEFAULT_LOG,
    window_days: int = DEFAULT_WINDOW_DAYS,
    min_triggers: int = DEFAULT_MIN_TRIGGERS,
    max_tasks: int = MAX_TASKS_PER_RUN,
    task_queue_obj: Any | None = None,
) -> dict:
    """Find repeat-breach SLOs and queue review tasks for up to max_tasks."""
    breaches = find_repeat_breaches(log_path=log_path, window_days=window_days, min_triggers=min_triggers)
    if not breaches:
        return {"created": [], "skipped": [], "found": 0}

    tq = task_queue_obj or _default_task_queue()
    if tq is None:
        return {"created": [], "skipped": [{"reason": "task_queue_unavailable"}], "found": len(breaches)}
    open_signatures = _open_signatures(tq)
    created: list[dict] = []
    skipped: list[dict] = []
    for breach in breaches[: max(1, int(max_tasks))]:
        sig = _signature(breach["slo"])
        if sig in open_signatures:
            skipped.append({"signature": sig, "reason": "open_task_exists"})
            continue
        task = tq.create_task(
            title=_task_title(breach),
            description=_task_description(breach),
            assigned_agent="brain_cli",
            priority=2,
            confidence=0.7,
            confidence_reasoning="repeat SLO breach — structural fix likely required",
            created_by="slo_replan",
            metadata={
                "domain": "brain-system",
                "source": "slo_replan",
                "replan_signature": sig,
                "slo": breach["slo"],
                "triggers": breach["triggers"],
                "window_days": breach["window_days"],
                "last_status": breach["last_status"],
                "last_current": breach["last_current"],
                "last_threshold": breach["last_threshold"],
                "mutates_policy": False,
                "uses_llm": True,
                "llm_dispatch": "cli_llm",
            },
        )
        created.append({"signature": sig, "task_id": task.get("id"), "slo": breach["slo"]})
        open_signatures.add(sig)
    return {"created": created, "skipped": skipped, "found": len(breaches)}


def _default_task_queue() -> Any | None:
    try:
        from task_queue import task_queue

        return task_queue
    except Exception as exc:
        log.debug("slo_replan: task_queue unavailable: %s", exc)
        return None


def _open_signatures(task_queue_obj: Any) -> set[str]:
    sigs: set[str] = set()
    try:
        for status in ("pending", "approved", "running"):
            for task in task_queue_obj.list_tasks(status=status) or []:
                md = task.get("metadata") or {}
                sig = md.get("replan_signature")
                if sig:
                    sigs.add(sig)
    except Exception as exc:
        log.debug("slo_replan: open-task scan failed: %s", exc)
    return sigs


def run_default() -> dict:
    return materialize_review_tasks()
