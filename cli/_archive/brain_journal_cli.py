#!/usr/bin/env python3
"""brain_journal_cli.py — inspect the brain_loop stream of consciousness.

Reads logs/brain_loop_journal.jsonl and prints recent ticks in a
human-readable format. This is the observability window into what brain is
"thinking" when nobody's looking — stalled goals, breaker trips, miss
patterns, idle quiet ticks, the whole internal monologue.

Usage:
  brain_journal_cli.py tail [--n 50]
  brain_journal_cli.py stats [--since 24h]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

JOURNAL = Path("/Users/chrischo/server/brain/logs/brain_loop_journal.jsonl")


def _parse_since(period: str) -> datetime:
    if not period:
        return datetime.now(UTC) - timedelta(days=1)
    m = re.match(r"^(\d+)([smhdw])$", period.strip())
    if not m:
        return datetime.now(UTC) - timedelta(days=1)
    n, unit = int(m.group(1)), m.group(2)
    delta = {
        "s": timedelta(seconds=n),
        "m": timedelta(minutes=n),
        "h": timedelta(hours=n),
        "d": timedelta(days=n),
        "w": timedelta(weeks=n),
    }[unit]
    return datetime.now(UTC) - delta


def _iter_lines() -> Iterator[dict]:
    if not JOURNAL.exists():
        return
    for line in JOURNAL.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def cmd_tail(args: argparse.Namespace) -> int:
    entries = list(_iter_lines())
    if not entries:
        print("(empty journal)")
        return 0
    for e in entries[-args.n :]:
        ts = (e.get("ts", "") or "")[:19].replace("T", " ")
        tick = e.get("tick", "?")
        obs_count = len(e.get("observations", []))
        approved = e.get("approved", 0)
        notes = e.get("notes", "")
        lat = e.get("latency_ms", "?")
        print(f"[{ts}] tick={tick} obs={obs_count} approved={approved} lat={lat}ms")
        if notes:
            print(f"    {notes}")
        for o in (e.get("observations", []) or [])[:5]:
            subj = (o.get("subject", "") or "")[:60]
            print(f"    · {o.get('kind','?'):20} {subj}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    cutoff = _parse_since(args.since)
    entries = list(_iter_lines())
    in_window = []
    for e in entries:
        ts_str = e.get("ts") or ""
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
        except ValueError:
            continue
        if ts >= cutoff:
            in_window.append(e)

    if not in_window:
        print(f"no ticks since {args.since}")
        return 0

    total_ticks = len(in_window)
    quiet_ticks = sum(1 for e in in_window if not e.get("observations"))
    total_obs = sum(len(e.get("observations", [])) for e in in_window)
    total_approved = sum(e.get("approved", 0) for e in in_window)
    latencies = [e.get("latency_ms", 0) for e in in_window if isinstance(e.get("latency_ms"), int)]
    latencies.sort()

    # Kind distribution
    kind_counts: dict[str, int] = {}
    for e in in_window:
        for o in e.get("observations", []) or []:
            k = o.get("kind", "?")
            kind_counts[k] = kind_counts.get(k, 0) + 1

    print(f"brain_loop journal stats — since {args.since}")
    print("=" * 60)
    print(f"ticks:            {total_ticks}")
    print(f"quiet ticks:      {quiet_ticks} ({quiet_ticks/total_ticks*100:.0f}%)")
    print(f"observations:     {total_obs}")
    print(f"approved actions: {total_approved}")
    if latencies:
        p50 = latencies[len(latencies) // 2]
        p95 = latencies[int(len(latencies) * 0.95)]
        print(f"latency p50:      {p50}ms")
        print(f"latency p95:      {p95}ms")
    print()
    print("observation kinds:")
    for k, v in sorted(kind_counts.items(), key=lambda x: -x[1]):
        print(f"  {k:<24} {v:>6}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="brain_journal_cli",
        description="Inspect brain_loop stream of consciousness journal.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_tail = sub.add_parser("tail", help="Print the last N journal entries")
    p_tail.add_argument("--n", type=int, default=20, help="How many ticks to show (default 20)")
    p_tail.set_defaults(func=cmd_tail)

    p_stats = sub.add_parser("stats", help="Summary stats over a time window")
    p_stats.add_argument("--since", default="24h", help="Window: 24h, 7d, 30m (default 24h)")
    p_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
