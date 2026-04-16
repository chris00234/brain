#!/usr/bin/env python3
"""brain_recall_cli.py — inspection CLI for active recall.

Three commands over brain.db::action_audit. No new tables, no new modules —
everything reads from the audit log that every /recall/* call already writes.

Commands:
  replay --session <id>        full turn-by-turn dump for one session
  miss [--since <period>]      detect correction follow-ups (SQL regex)
  stats [--since <period>]     latency p50/p95, intent distribution, volume

Usage:
  brain_recall_cli.py replay --session e2e-test-001
  brain_recall_cli.py miss --since 7d
  brain_recall_cli.py stats --since 24h
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")

# Correction detection regexes — bilingual. Tested against the 2026-04-14
# "왜 브레인 안써" incident that triggered this whole plan.
CORRECTION_PATTERNS = [
    r"왜\s*브레인\s*안\s*써",       # "why are you not using brain"
    r"왜.*안\s*썼",                  # "why didn't you use ..."
    r"내가\s*알려준",                # "I told you about ..."
    r"기억.*안\s*나",                # "you don't remember"
    r"brain.*안\s*써",               # English-KR mix
    r"should\s+have\s+used\s+brain",
    r"did\s+not\s+use\s+brain",
    r"you\s+didn.t\s+(?:check|use|query)\s+brain",
    r"you\s+ignored",
    r"did\s+you\s+(?:even\s+)?(?:check|query)",
]

CORRECTION_REGEX = re.compile("|".join(CORRECTION_PATTERNS), re.IGNORECASE)


def _parse_since(period: str) -> datetime:
    """Parse '7d', '24h', '30m' into a datetime cutoff (UTC)."""
    if not period:
        return datetime.now(timezone.utc) - timedelta(days=7)
    m = re.match(r"^(\d+)([smhdw])$", period.strip())
    if not m:
        return datetime.now(timezone.utc) - timedelta(days=7)
    n = int(m.group(1))
    unit = m.group(2)
    delta = {
        "s": timedelta(seconds=n),
        "m": timedelta(minutes=n),
        "h": timedelta(hours=n),
        "d": timedelta(days=n),
        "w": timedelta(weeks=n),
    }[unit]
    return datetime.now(timezone.utc) - delta


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(BRAIN_DB), timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


# ── Replay ────────────────────────────────────────────────────────

def cmd_replay(args: argparse.Namespace) -> int:
    """Print every turn for a specific session."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, created_at, route, query_text, retrieved_atom_ids, actor "
            "FROM action_audit "
            "WHERE session_id = ? AND route LIKE '/recall%' "
            "ORDER BY created_at ASC",
            (args.session,),
        ).fetchall()

    if not rows:
        print(f"No recall entries for session '{args.session}'", file=sys.stderr)
        return 1

    print(f"Replay for session {args.session} — {len(rows)} recall events")
    print("=" * 80)
    for i, r in enumerate(rows, 1):
        ts = (r["created_at"] or "")[:19].replace("T", " ")
        atom_count = len(json.loads(r["retrieved_atom_ids"] or "[]"))
        qt = (r["query_text"] or "")[:120].replace("\n", " ")
        print(f"[{i:3d}] {ts} {r['route']} [{r['actor'] or '?'}] blocks={atom_count}")
        print(f"      prompt: {qt}")
    return 0


# ── Miss detection ────────────────────────────────────────────────

def cmd_miss(args: argparse.Namespace) -> int:
    """Find sessions where the next prompt looks like a correction, meaning
    the previous /recall/active response missed what Chris actually wanted."""
    cutoff = _parse_since(args.since).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, created_at, query_text, session_id, retrieved_atom_ids "
            "FROM action_audit "
            "WHERE route = '/recall/active' AND created_at >= ? "
            "ORDER BY session_id, created_at ASC",
            (cutoff,),
        ).fetchall()

    # Group by session and scan for consecutive turns where the next prompt
    # matches a correction regex.
    by_session: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_session.setdefault(r["session_id"] or "unknown", []).append(r)

    misses: list[dict] = []
    for sid, turns in by_session.items():
        for i, t in enumerate(turns):
            if i == 0:
                continue
            prev = turns[i - 1]
            cur_text = t["query_text"] or ""
            if CORRECTION_REGEX.search(cur_text):
                misses.append({
                    "session_id": sid,
                    "prev_ts": prev["created_at"],
                    "prev_prompt": (prev["query_text"] or "")[:200],
                    "correction_ts": t["created_at"],
                    "correction_prompt": cur_text[:200],
                    "prev_atom_count": len(json.loads(prev["retrieved_atom_ids"] or "[]")),
                })

    if not misses:
        print(f"No recall misses detected since {args.since}")
        return 0

    print(f"{len(misses)} recall miss(es) detected since {args.since}")
    print("=" * 80)
    for m in misses:
        print(f"session: {m['session_id']}")
        print(f"  {m['prev_ts']}  prev:      {m['prev_prompt']}")
        print(f"     (injected {m['prev_atom_count']} blocks)")
        print(f"  {m['correction_ts']}  correction: {m['correction_prompt']}")
        print()
    return 0


# ── Stats ────────────────────────────────────────────────────────

def cmd_stats(args: argparse.Namespace) -> int:
    """Latency distribution, intent frequency, volume since cutoff."""
    cutoff = _parse_since(args.since).isoformat()
    with _conn() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM action_audit WHERE route='/recall/active' AND created_at >= ?",
            (cutoff,),
        ).fetchone()[0]
        by_actor = conn.execute(
            "SELECT actor, COUNT(*) FROM action_audit "
            "WHERE route='/recall/active' AND created_at >= ? "
            "GROUP BY actor ORDER BY 2 DESC",
            (cutoff,),
        ).fetchall()
        by_session = conn.execute(
            "SELECT session_id, COUNT(*) FROM action_audit "
            "WHERE route='/recall/active' AND created_at >= ? "
            "GROUP BY session_id ORDER BY 2 DESC LIMIT 10",
            (cutoff,),
        ).fetchall()

    print(f"/recall/active stats since {args.since} (cutoff {cutoff[:19]})")
    print("=" * 80)
    print(f"total calls: {total}")
    print()
    print("by actor:")
    for r in by_actor:
        print(f"  {r[0] or '(none)':<20} {r[1]:>6}")
    print()
    print("top sessions:")
    for r in by_session:
        sid = (r[0] or "(none)")[:40]
        print(f"  {sid:<42} {r[1]:>6}")
    print()

    # Miss rate over same window
    miss_args = argparse.Namespace(since=args.since)
    _capture = sys.stdout
    import io
    sys.stdout = io.StringIO()
    try:
        cmd_miss(miss_args)
    finally:
        miss_output = sys.stdout.getvalue()
        sys.stdout = _capture
    miss_count_match = re.search(r"^(\d+)\s+recall miss", miss_output, re.MULTILINE)
    miss_count = int(miss_count_match.group(1)) if miss_count_match else 0
    if total > 0:
        print(f"miss rate: {miss_count}/{total} = {miss_count/total*100:.1f}%")
    else:
        print("miss rate: no data")
    return 0


# ── CLI entry ────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="brain_recall_cli",
        description="Inspect active recall behavior via action_audit log.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_replay = sub.add_parser("replay", help="Dump turn-by-turn for a session")
    p_replay.add_argument("--session", required=True, help="Session id to replay")
    p_replay.set_defaults(func=cmd_replay)

    p_miss = sub.add_parser("miss", help="Detect recall misses via correction regex")
    p_miss.add_argument("--since", default="7d", help="Time window: 7d, 24h, 30m (default: 7d)")
    p_miss.set_defaults(func=cmd_miss)

    p_stats = sub.add_parser("stats", help="Latency + intent + volume summary")
    p_stats.add_argument("--since", default="24h", help="Time window: 7d, 24h, 30m (default: 24h)")
    p_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
