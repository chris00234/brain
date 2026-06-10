#!/usr/bin/env python3
"""Convert persistent extended-eval failures into review tasks.

Background: extended eval (605 queries, nightly) sits at 80% accuracy with
~115 queries that fail every single run. The eval_regression_diff job
already labels them as ``persistent_failures`` per run, but nothing reads
that signal — the 115 ids are just sitting in jsonl history. Without
a triage path the hard-query backlog can never shrink.

This job:
  1. Reads the last N eval-regression-diff rows from the extended track.
  2. Selects queries that appear in ``persistent_failures`` for >= N runs.
  3. Maps each id back to its query text via the eval_set file (case_id
     scheme: `q_<sha1(query)[:12]>` — same hash eval_gate uses).
  4. Creates a bounded set of review tasks via task_queue.

Dedupes by stable signature (sha of case_id) so daily runs don't multiply
tasks. No LLM. Read-only against the eval set; only writes are the new
review tasks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

log = logging.getLogger("brain.eval_persistent_failures_triage")

BRAIN_ROOT = Path("/Users/chrischo/server/brain")
LOGS_DIR = BRAIN_ROOT / "logs"
DEFAULT_DIFF_FILE = LOGS_DIR / "eval-regression-diff-extended.jsonl"
DEFAULT_EVAL_SET = BRAIN_ROOT / "cli" / "eval_set_extended.json"

DEFAULT_LOOKBACK_RUNS = 3
DEFAULT_MAX_TASKS = 3
DEFAULT_MIN_PERSIST_COUNT = 3


def _case_id(query: str) -> str:
    return "q_" + hashlib.sha1(query.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


def _load_recent_diffs(diff_file: Path, lookback: int) -> list[dict]:
    if not diff_file.exists():
        return []
    rows: list[dict] = []
    try:
        for line in reversed(diff_file.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("status") == "ok":
                rows.append(row)
                if len(rows) >= lookback:
                    break
    except OSError:
        return []
    return rows


def _persistent_ids_across_runs(diffs: list[dict], min_count: int) -> dict[str, int]:
    """Count occurrences of each id in `persistent_failures` across recent runs."""
    counts: dict[str, int] = {}
    for diff in diffs:
        for qid in diff.get("persistent_failures") or []:
            counts[str(qid)] = counts.get(str(qid), 0) + 1
    return {qid: c for qid, c in counts.items() if c >= min_count}


def _build_query_map(eval_set: Path) -> dict[str, dict]:
    if not eval_set.exists():
        return {}
    try:
        data = json.loads(eval_set.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, list):
        return {}
    out: dict[str, dict] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        query = (entry.get("query") or "").strip()
        if not query:
            continue
        out[_case_id(query)] = {
            "query": query,
            "collection": entry.get("collection") or "all",
            "expected_source": entry.get("expected_source") or "",
            "expected_content": entry.get("expected_content") or "",
        }
    return out


def _open_task_signatures(task_queue_obj: Any) -> set[str]:
    sigs: set[str] = set()
    try:
        rows = task_queue_obj.list_tasks(status_in=("pending", "approved", "running"), limit=200)
    except Exception:
        return sigs
    for row in rows or []:
        try:
            meta = row.get("metadata") if isinstance(row, dict) else None
            if not meta:
                continue
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except json.JSONDecodeError:
                    continue
            sig = meta.get("eval_failure_signature") if isinstance(meta, dict) else None
            if sig:
                sigs.add(str(sig))
        except Exception:  # noqa: S112 — malformed task row, skip
            continue
    return sigs


def _default_task_queue() -> Any | None:
    try:
        from task_queue import task_queue

        return task_queue
    except Exception:
        try:
            from brain_core.task_queue import task_queue  # type: ignore[no-redef]

            return task_queue
        except Exception:
            return None


def create_persistent_failure_tasks(
    *,
    diff_file: Path = DEFAULT_DIFF_FILE,
    eval_set: Path = DEFAULT_EVAL_SET,
    lookback_runs: int = DEFAULT_LOOKBACK_RUNS,
    min_persist_count: int = DEFAULT_MIN_PERSIST_COUNT,
    max_tasks: int = DEFAULT_MAX_TASKS,
    dry_run: bool = False,
    task_queue_obj: Any | None = None,
) -> dict:
    diffs = _load_recent_diffs(diff_file, lookback_runs)
    if not diffs:
        return {"status": "no_history", "diff_file": str(diff_file)}

    persistent_counts = _persistent_ids_across_runs(diffs, min_persist_count)
    if not persistent_counts:
        return {"status": "no_persistent", "examined_runs": len(diffs)}

    query_map = _build_query_map(eval_set)

    tq = task_queue_obj or _default_task_queue()
    if tq is None and not dry_run:
        return {"status": "task_queue_unavailable"}

    open_sigs = _open_task_signatures(tq) if tq else set()

    # Rank by persistence count desc — most-stuck first.
    ranked = sorted(persistent_counts.items(), key=lambda kv: kv[1], reverse=True)
    created: list[dict] = []
    skipped: list[dict] = []
    for qid, count in ranked:
        signature = "eval_persistent_" + hashlib.sha1(qid.encode(), usedforsecurity=False).hexdigest()[:10]
        if signature in open_sigs:
            skipped.append({"qid": qid, "reason": "open_task_exists"})
            continue
        if len(created) >= max_tasks:
            break
        entry = query_map.get(qid) or {}
        query_text = entry.get("query") or qid
        title = f"Investigate persistent eval failure: {query_text[:72]}"
        description = (
            f"Extended eval query `{qid}` failed in the last {count} consecutive runs.\n\n"
            f"Query: {query_text}\n"
            f"Expected source: {entry.get('expected_source') or 'unknown'}\n"
            f"Expected content: {entry.get('expected_content') or 'unknown'}\n"
            f"Collection: {entry.get('collection') or 'all'}\n\n"
            "Investigate whether the chunk is missing from the index, mis-tagged, "
            "or losing to a sibling on rerank. Treat this as evidence for the "
            "extended-eval mediocrity weak point, not an isolated bug."
        )
        if dry_run:
            created.append({"qid": qid, "persist_count": count, "dry_run": True, "title": title})
            continue
        task = tq.create_task(
            title=title,
            description=description,
            assigned_agent="brain_cli",
            priority=4,
            confidence=0.0,
            confidence_reasoning="eval_persistent_failures_triage — query failed >=N consecutive runs",
            created_by="eval_persistent_failures_triage",
            metadata={
                "domain": "brain-system",
                "source": "eval_persistent_failures_triage",
                "eval_failure_signature": signature,
                "case_id": qid,
                "persist_count": count,
                "query": query_text,
                "expected_source": entry.get("expected_source") or "",
                "expected_content": entry.get("expected_content") or "",
                "collection": entry.get("collection") or "all",
                "mutates_policy": False,
                "uses_llm": False,
            },
        )
        created.append({"qid": qid, "persist_count": count, "task_id": task.get("id")})

    return {
        "status": "ok",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "examined_runs": len(diffs),
        "persistent_count": len(persistent_counts),
        "created": created,
        "skipped": skipped,
        "min_persist_count": min_persist_count,
        "lookback_runs": lookback_runs,
        "max_tasks": max_tasks,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--diff-file", type=Path, default=DEFAULT_DIFF_FILE)
    p.add_argument("--eval-set", type=Path, default=DEFAULT_EVAL_SET)
    p.add_argument("--lookback-runs", type=int, default=DEFAULT_LOOKBACK_RUNS)
    p.add_argument("--min-persist-count", type=int, default=DEFAULT_MIN_PERSIST_COUNT)
    p.add_argument("--max-tasks", type=int, default=DEFAULT_MAX_TASKS)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    result = create_persistent_failure_tasks(
        diff_file=args.diff_file,
        eval_set=args.eval_set,
        lookback_runs=args.lookback_runs,
        min_persist_count=args.min_persist_count,
        max_tasks=args.max_tasks,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
