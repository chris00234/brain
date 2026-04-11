#!/Users/chrischo/server/brain/.venv/bin/python3
"""Knowledge gap detector.

Reads `logs/recall-gaps.jsonl` (written by /recall on empty/low-quality
results), groups by normalized query, and creates a `knowledge_gap` task in
the task_queue when the same query repeats ≥3 times in a 14-day window.

Tracks a high-watermark in `logs/gap-detector-state.json` so re-runs don't
double-count old gap rows.
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

GAP_LOG = Path("/Users/chrischo/server/brain/logs/recall-gaps.jsonl")
STATE_FILE = Path("/Users/chrischo/server/brain/logs/gap-detector-state.json")
WINDOW_DAYS = 14
MIN_REPEAT = 3
NORMALIZE_RE = re.compile(r"[^\w\s]+", re.UNICODE)


def _normalize(query: str) -> str:
    """Lowercase + strip punctuation + collapse whitespace."""
    cleaned = NORMALIZE_RE.sub(" ", (query or "").lower())
    return " ".join(cleaned.split())


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"high_watermark": "", "promoted_queries": {}}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"high_watermark": "", "promoted_queries": {}}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def main() -> int:
    if not GAP_LOG.exists():
        print(json.dumps({"status": "no_gap_log", "tasks_created": 0}))
        return 0

    state = _load_state()
    high_watermark = state.get("high_watermark", "")
    promoted = state.get("promoted_queries", {})  # normalized → last_promoted_iso
    cutoff = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)

    # Read every entry, group by normalized query, ignore entries older than the window.
    by_query: dict[str, list[dict]] = defaultdict(list)
    new_max_ts = high_watermark
    with GAP_LOG.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            ts = row.get("timestamp", "")
            if not ts:
                continue
            try:
                row_dt = datetime.fromisoformat(ts.rstrip("Zz"))
                if row_dt.tzinfo is None:
                    row_dt = row_dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if row_dt < cutoff:
                continue
            if ts > new_max_ts:
                new_max_ts = ts
            normalized = _normalize(row.get("query", ""))
            if not normalized:
                continue
            by_query[normalized].append(row)

    # For any query that meets the threshold AND wasn't promoted in the
    # current window, create a task.
    tasks_created = 0
    try:
        from task_queue import TaskQueue  # type: ignore
        tq = TaskQueue()
    except Exception as e:
        print(json.dumps({"status": "error", "reason": f"task_queue import failed: {e}"}))
        return 2

    for normalized, rows in sorted(by_query.items(), key=lambda kv: -len(kv[1])):
        if len(rows) < MIN_REPEAT:
            continue
        # If we already promoted this query within the window, skip.
        last_promoted = promoted.get(normalized, "")
        if last_promoted:
            try:
                lp_dt = datetime.fromisoformat(last_promoted.rstrip("Zz"))
                if lp_dt.tzinfo is None:
                    lp_dt = lp_dt.replace(tzinfo=timezone.utc)
                if lp_dt >= cutoff:
                    continue
            except Exception:
                pass
        sample_query = rows[0].get("query", normalized)
        n_total = len(rows)
        max_score = max(float(r.get("max_score", 0)) for r in rows)
        try:
            tq.create_task(
                title=f"Knowledge gap: {sample_query[:80]}",
                description=(
                    f"This query has returned poor results {n_total} times in the last "
                    f"{WINDOW_DAYS} days (max score {max_score:.1f}). Find or ingest a "
                    f"source that answers it.\n\nNormalized: {normalized}"
                ),
                assigned_agent="jenna",
                priority=7,
                confidence=0.7,
                confidence_reasoning=f"Triggered by {n_total} low-quality recall events",
                created_by="gap_detector",
                metadata={
                    "gap_query": sample_query,
                    "normalized": normalized,
                    "occurrence_count": n_total,
                    "max_score_seen": max_score,
                    "window_days": WINDOW_DAYS,
                },
            )
            promoted[normalized] = datetime.now(timezone.utc).isoformat()
            tasks_created += 1
        except Exception as e:
            print(f"  task creation failed for '{normalized[:40]}': {e}", file=sys.stderr)

    state["high_watermark"] = new_max_ts
    state["promoted_queries"] = promoted
    _save_state(state)

    print(json.dumps({
        "status": "ok",
        "tasks_created": tasks_created,
        "queries_inspected": len(by_query),
        "window_days": WINDOW_DAYS,
        "min_repeat": MIN_REPEAT,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
