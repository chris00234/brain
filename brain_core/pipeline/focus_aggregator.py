#!/Users/chrischo/server/brain/.venv/bin/python3
"""Focus / energy data layer (Round 9 B4).

Aggregates Chris's daily activity into a (day-of-week × hour-of-day) grid.
This is the *data layer only* — no prediction model. We need ≥4 weeks of
this data before training anything.

Inputs:
- Inbox records of source_type=screen_time (day-level summaries)
- Inbox records of source_type=git_activity (day-level summaries)
- Direct `git log` output from tracked repos (gives true commit timestamps,
  unlike the day-bucketed inbox records)

Output:
- /Users/chrischo/server/brain/logs/focus-aggregate.jsonl
  One row per (day, hour) bucket with screen_time_minutes (or None when
  data only has day-level granularity), commit_count, repo_count, and
  rolling 4-week aggregates per (dow, hour).

Run via the `focus_aggregate` cron job, daily at 4:35am.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

INBOX_DIR = Path("/Users/chrischo/server/knowledge/raw/inbox")
OUTPUT_FILE = Path("/Users/chrischo/server/brain/logs/focus-aggregate.jsonl")
WINDOW_DAYS = 28  # 4 weeks rolling

# Same repo list as ingest/git_activity.py — keep in sync.
REPOS: list[Path] = [
    Path.home() / "server/brain",
    Path.home() / "server/brain-ui",
    Path.home() / "server/chrischodev",
    Path.home() / "server/claw3d",
    Path.home() / "server/knowledge",
    Path.home() / "LibreUIUX-Claude-Code",
    Path.home() / "jenna_teacher",
    Path.home() / "oc-lifehub",
    Path.home() / "ui-ux-pro-max-skill",
]

DOW_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def collect_git_commits_with_timestamps(since: datetime) -> list[tuple[datetime, str, str]]:
    """Run `git log --format=%aI|%an|%h` per repo. Returns (commit_dt, repo, sha)."""
    out: list[tuple[datetime, str, str]] = []
    since_iso = since.strftime("%Y-%m-%d")
    for repo in REPOS:
        if not repo.exists() or not (repo / ".git").exists():
            continue
        try:
            result = subprocess.run(
                ["git", "log", f"--since={since_iso}", "--all", "--format=%aI|%h"],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                continue
            for line in result.stdout.splitlines():
                if "|" not in line:
                    continue
                ts_raw, sha = line.split("|", 1)
                try:
                    dt = datetime.fromisoformat(ts_raw.strip())
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                out.append((dt, repo.name, sha.strip()))
        except Exception:
            continue
    return out


def collect_screentime_days(since: datetime) -> dict[str, float]:
    """Read screentime inbox records — return {date_str: total_minutes_estimate}.

    Records have free-text content like "Chrome 1h14m, Finder 24m, Terminal 5m".
    Cheap regex: sum all "Nh" + "Nm" mentions in the content blob.
    """
    import re
    out: dict[str, float] = {}
    if not INBOX_DIR.exists():
        return out
    pattern = re.compile(r"(\d+)\s*h\s*(\d+)?\s*m?|(\d+)\s*m\b")
    for f in INBOX_DIR.glob("raw_screentime_*.json"):
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        ts = data.get("timestamp", "")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.rstrip("Zz"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if dt < since:
            continue
        date_key = dt.strftime("%Y-%m-%d")
        content = data.get("content", "") or ""
        minutes = 0.0
        for m in pattern.finditer(content):
            h_str, m1_str, m2_str = m.groups()
            if h_str:
                minutes += int(h_str) * 60
                if m1_str:
                    minutes += int(m1_str)
            elif m2_str:
                minutes += int(m2_str)
        out[date_key] = out.get(date_key, 0.0) + minutes
    return out


def main() -> int:
    print(f"[focus_aggregate] starting at {datetime.now(timezone.utc).isoformat()}", flush=True)
    since = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)

    # ── Hour-of-day grid from git commits (real timestamps) ──
    commits = collect_git_commits_with_timestamps(since)
    print(f"[focus_aggregate] collected {len(commits)} commits in last {WINDOW_DAYS}d", flush=True)

    # Bucket: (date, hour) → commit_count
    by_date_hour: dict[tuple[str, int], int] = defaultdict(int)
    by_dow_hour: dict[tuple[int, int], int] = defaultdict(int)
    for commit_dt, repo, sha in commits:
        # Convert to local time so hour-of-day matches Chris's wall clock
        local = commit_dt.astimezone()
        date_key = local.strftime("%Y-%m-%d")
        hour = local.hour
        dow = local.weekday()  # 0=Mon
        by_date_hour[(date_key, hour)] += 1
        by_dow_hour[(dow, hour)] += 1

    # ── Day-level screen_time (no hourly breakdown available) ──
    screentime_by_day = collect_screentime_days(since)
    print(f"[focus_aggregate] {len(screentime_by_day)} days have screentime data", flush=True)

    # ── Build output rows ──
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.now(timezone.utc).isoformat()

    # Daily rows: one per (date, hour) cell that has any activity, plus
    # one summary row per day with the screen_time total.
    row_count = 0
    rows: list[dict] = []

    # Per-(date, hour) commits
    for (date_key, hour), count in sorted(by_date_hour.items()):
        try:
            d = datetime.strptime(date_key, "%Y-%m-%d")
            dow = d.weekday()
        except Exception:
            continue
        rows.append({
            "kind": "commits_hour",
            "date": date_key,
            "dow": DOW_NAMES[dow],
            "hour": hour,
            "commit_count": count,
            "computed_at": now_iso,
        })
        row_count += 1

    # Per-day screen time totals
    for date_key, minutes in sorted(screentime_by_day.items()):
        try:
            d = datetime.strptime(date_key, "%Y-%m-%d")
            dow = d.weekday()
        except Exception:
            continue
        rows.append({
            "kind": "screentime_day",
            "date": date_key,
            "dow": DOW_NAMES[dow],
            "minutes": round(minutes, 1),
            "computed_at": now_iso,
        })
        row_count += 1

    # Rolling (dow, hour) aggregates — the actual "best work window" signal.
    for (dow, hour), total in sorted(by_dow_hour.items()):
        rows.append({
            "kind": "dow_hour_rollup",
            "dow": DOW_NAMES[dow],
            "hour": hour,
            "commit_count_total": total,
            "window_days": WINDOW_DAYS,
            "computed_at": now_iso,
        })
        row_count += 1

    # Write atomically
    tmp = OUTPUT_FILE.with_suffix(".tmp")
    with tmp.open("w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, OUTPUT_FILE)

    # Top-3 productive hour windows for the log line
    top_cells = sorted(by_dow_hour.items(), key=lambda kv: -kv[1])[:3]
    top_summary = ", ".join(
        f"{DOW_NAMES[d]}@{h:02d}h={c}" for (d, h), c in top_cells
    )
    print(f"[focus_aggregate] wrote {row_count} rows; top productive cells: {top_summary}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
