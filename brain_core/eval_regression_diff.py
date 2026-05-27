"""brain_core/eval_regression_diff.py — surface eval-extended regressions.

The extended eval (605 cases, nightly) drifts in the 79-81% accuracy band; a
4-case swing is significant but invisible from the rolled-up pass/fail counts
alone. Without a per-test diff, "which queries regressed?" requires re-running
the eval.

`eval_gate._persist_eval_report` now appends `failed_ids` to each history
row. This module reads the last two rows from the chosen track's history file
and computes the regression delta:

  newly_failing : ids that passed yesterday and failed today
  newly_passing : ids that failed yesterday and passed today
  persistent    : ids that failed in both runs

Designed to run after the eval cron writes its row, idempotent, no LLM,
read-only against the history file.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("brain.eval_regression_diff")

BRAIN_ROOT = Path("/Users/chrischo/server/brain")
LOGS_DIR = BRAIN_ROOT / "logs"


def _history_path(track: str) -> Path:
    if track == "default":
        return LOGS_DIR / "eval-history.jsonl"
    return LOGS_DIR / f"eval-history-{track}.jsonl"


def _last_two_runs(track: str) -> tuple[dict | None, dict | None]:
    """Return (previous, current) eval history rows, oldest first."""
    path = _history_path(track)
    if not path.exists():
        return None, None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None, None
    rows: list[dict] = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and "failed_ids" in row:
            rows.append(row)
            if len(rows) == 2:
                break
    if not rows:
        return None, None
    if len(rows) == 1:
        return None, rows[0]
    return rows[1], rows[0]  # oldest-first


def compute_diff(track: str = "extended") -> dict:
    """Compare the two most recent runs of `track` and emit a regression delta."""
    prev, curr = _last_two_runs(track)
    if curr is None:
        return {"status": "no_history", "track": track}
    if prev is None:
        return {
            "status": "single_run",
            "track": track,
            "current_failed": len(curr.get("failed_ids") or []),
        }
    prev_failed = set(prev.get("failed_ids") or [])
    curr_failed = set(curr.get("failed_ids") or [])
    newly_failing = sorted(curr_failed - prev_failed)
    newly_passing = sorted(prev_failed - curr_failed)
    persistent = sorted(prev_failed & curr_failed)
    return {
        "status": "ok",
        "track": track,
        "computed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "previous_timestamp": prev.get("timestamp"),
        "current_timestamp": curr.get("timestamp"),
        "previous_accuracy": prev.get("accuracy"),
        "current_accuracy": curr.get("accuracy"),
        "delta_accuracy": (
            round(curr.get("accuracy", 0) - prev.get("accuracy", 0), 2)
            if isinstance(curr.get("accuracy"), (int, float))
            and isinstance(prev.get("accuracy"), (int, float))
            else None
        ),
        "newly_failing": newly_failing,
        "newly_passing": newly_passing,
        "persistent_failures": persistent,
        "summary": (
            f"{len(newly_failing)} new fail, {len(newly_passing)} recovered, " f"{len(persistent)} persistent"
        ),
    }


# If the most recent eval-history row is older than this, the diff job
# considers the run "not_ready". Sized just under the 24h cron interval:
# the diff cron runs ~25 min after eval_run_extended finishes, so today's
# row is always < 1h old when this check fires; a missed/failed upstream
# eval leaves yesterday's row at ~24h+ which trips the gate. 36h+ would
# silently accept yesterday's run as "today" if upstream stalls.
FRESHNESS_THRESHOLD_HOURS = 20


def _is_fresh(row: dict | None, threshold_hours: float = FRESHNESS_THRESHOLD_HOURS) -> bool:
    if not row:
        return False
    raw = row.get("timestamp")
    if not raw:
        return False
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    age_hours = (datetime.now(UTC) - ts).total_seconds() / 3600.0
    return age_hours <= threshold_hours


def _already_recorded(track: str, prev_ts: str, curr_ts: str) -> bool:
    """Has this exact (previous, current) pair already been written?"""
    out_path = LOGS_DIR / f"eval-regression-diff-{track}.jsonl"
    if not out_path.exists():
        return False
    try:
        for line in reversed(out_path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(row, dict)
                and row.get("previous_timestamp") == prev_ts
                and row.get("current_timestamp") == curr_ts
            ):
                return True
    except OSError:
        return False
    return False


def write_diff_record(track: str = "extended") -> dict:
    """Compute the diff and append it to eval-regression-diff-<track>.jsonl.

    Refuses to write under two conditions:
      * `not_ready` — the most recent history row is older than
        FRESHNESS_THRESHOLD_HOURS, so the upstream eval probably failed or
        hasn't produced today's row yet. Publishing a "diff" of yesterday-vs-day-
        before would mislead triage.
      * `duplicate` — the same (previous, current) pair was already recorded.
        The job is idempotent; repeated cron firings against unchanged history
        should not multiply rows.
    """
    diff = compute_diff(track)
    if diff.get("status") != "ok":
        return diff
    prev, curr = _last_two_runs(track)
    if not _is_fresh(curr):
        diff["status"] = "not_ready"
        diff["write_status"] = "skipped"
        diff["reason"] = (
            f"current history row older than {FRESHNESS_THRESHOLD_HOURS}h — "
            "upstream eval likely missing or stuck"
        )
        return diff
    prev_ts = str(prev.get("timestamp") or "") if prev else ""
    curr_ts = str(curr.get("timestamp") or "") if curr else ""
    if _already_recorded(track, prev_ts, curr_ts):
        diff["write_status"] = "skipped"
        diff["reason"] = "duplicate_pair_already_recorded"
        return diff
    out_path = LOGS_DIR / f"eval-regression-diff-{track}.jsonl"
    try:
        with out_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(diff, ensure_ascii=False) + "\n")
    except OSError as exc:
        log.warning("eval-regression-diff write failed: %s", exc)
        diff["write_status"] = "error"
    else:
        diff["write_status"] = "ok"
    return diff


if __name__ == "__main__":
    import argparse
    import sys

    p = argparse.ArgumentParser()
    p.add_argument("--track", default="extended")
    p.add_argument("--no-write", action="store_true")
    args = p.parse_args()
    out = compute_diff(args.track) if args.no_write else write_diff_record(args.track)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    # Surface failures to the scheduler:
    #  * write_status=error → disk/perm failure during JSONL append
    #  * status=not_ready → upstream eval missing or stuck (freshness gate)
    # Other statuses (no_history, single_run, duplicate_pair) are
    # informational — there's nothing to act on, but it's not a failure.
    if out.get("write_status") == "error":
        sys.exit(1)
    if out.get("status") == "not_ready":
        sys.exit(1)
