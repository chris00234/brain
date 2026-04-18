#!/opt/homebrew/bin/python3
"""Bench-all regression discipline (2026-04-17).

Inspired by friend's `bench_all` pattern: every non-trivial search-pipeline
change requires a before/after eval comparison. Running this:

  1. Snapshots current stable eval + SLO state to /tmp/brain_bench_before.json
  2. Waits for you to make + deploy the change
  3. Running again compares before → after and reports the diff

Usage:
  # Capture baseline before making changes
  bench_all.py snap before

  # Apply your change, restart brain-server, trigger eval
  launchctl kickstart -k gui/$UID/ai.openclaw.brain-server
  curl -s -X POST -H "Authorization: Bearer $SECRET" /jobs/eval_run
  # ... wait for eval to finish ...

  # Capture and diff
  bench_all.py snap after
  bench_all.py diff

Exit codes:
  0 — no regression (content_hit within 2pt of baseline)
  1 — regression detected (>2pt drop) — review before shipping
  2 — snapshot file missing

Non-destructive. Idempotent. No backend writes.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

log = logging.getLogger("brain.bench_all")

SNAP_BEFORE = Path("/tmp/brain_bench_before.json")
SNAP_AFTER = Path("/tmp/brain_bench_after.json")
EVAL_REPORT = Path("/Users/chrischo/server/brain/logs/eval-report-stable.json")


def load_report() -> dict:
    if not EVAL_REPORT.exists():
        raise SystemExit(f"eval report missing: {EVAL_REPORT}")
    d = json.loads(EVAL_REPORT.read_text())
    v2 = d.get("v2", {})
    per_test = v2.get("per_test", [])
    # v2 envelope's top-level `failed`/`passed` fields are sometimes None —
    # derive from per_test so we always get an accurate count.
    fail_queries = [t.get("query", "")[:60] for t in per_test if not t.get("hit_content")]
    pass_count = sum(1 for t in per_test if t.get("hit_content"))
    return {
        "timestamp": d.get("timestamp"),
        "hit_content_pct": v2.get("hit_content_pct", 0),
        "hit_source_pct": v2.get("hit_source_pct", 0),
        "hit_content_loose_pct": v2.get("hit_content_loose_pct", 0),
        "mrr": v2.get("mrr", 0),
        "ndcg5": v2.get("ndcg5", 0),
        "mean_latency_ms": v2.get("mean_latency_ms", 0),
        "total": v2.get("total", 0) or len(per_test),
        "passed": pass_count,
        "failed": len(fail_queries),
        "fail_queries": fail_queries,
    }


def cmd_snap(label: str) -> int:
    target = SNAP_BEFORE if label == "before" else SNAP_AFTER
    snap = load_report()
    # 2026-04-17 fix: staleness guard. Today's session had a false 59.4%
    # regression because snap captured a cold-start eval from 2h earlier.
    # Warn loudly if the report we're snapping is older than 10 min so the
    # operator re-runs eval_run instead of chasing a phantom regression.
    try:
        from datetime import datetime as _dt

        age_s = (_dt.now() - _dt.fromisoformat(str(snap["timestamp"]).split("+")[0])).total_seconds()
        if age_s > 600:
            mins = int(age_s // 60)
            print(
                f"  ⚠ WARNING: eval report is {mins} min stale. Run eval_run before trusting this snapshot."
            )
    except Exception as _exc:
        log.debug("silenced exception in bench_all.py: %s", _exc)
    target.write_text(json.dumps(snap, indent=2, ensure_ascii=False))
    print(f"snapshot [{label}] saved: {target}")
    print(f"  eval @ {snap['timestamp']}")
    print(f"  content_hit = {snap['hit_content_pct']}%")
    print(f"  source_hit  = {snap['hit_source_pct']}%")
    print(f"  mrr         = {snap['mrr']}")
    print(f"  latency     = {snap['mean_latency_ms']}ms")
    print(f"  fails       = {snap['failed']}")
    return 0


def _fmt_delta(before: float, after: float, unit: str = "", bigger_is_better: bool = True) -> str:
    delta = after - before
    arrow = "→"
    if delta == 0:
        tag = "·"
    elif (delta > 0) == bigger_is_better:
        tag = "✓"
    else:
        tag = "✗"
    sign = "+" if delta > 0 else ""
    return f"{before}{unit} {arrow} {after}{unit}  ({tag} {sign}{delta:.2f}{unit})"


def cmd_diff() -> int:
    if not SNAP_BEFORE.exists():
        print(f"ERROR: before snapshot missing at {SNAP_BEFORE}")
        return 2
    if not SNAP_AFTER.exists():
        print(f"ERROR: after snapshot missing at {SNAP_AFTER}")
        return 2

    before = json.loads(SNAP_BEFORE.read_text())
    after = json.loads(SNAP_AFTER.read_text())

    print("=" * 60)
    print("BENCH DIFF")
    print("=" * 60)
    print(f"before: {before['timestamp']}")
    print(f"after:  {after['timestamp']}")
    print()
    print(f"  content_hit_pct    {_fmt_delta(before['hit_content_pct'], after['hit_content_pct'], '%')}")
    print(f"  source_hit_pct     {_fmt_delta(before['hit_source_pct'], after['hit_source_pct'], '%')}")
    print(
        f"  hit_content_loose  {_fmt_delta(before['hit_content_loose_pct'], after['hit_content_loose_pct'], '%')}"
    )
    print(f"  mrr                {_fmt_delta(before['mrr'], after['mrr'], '')}")
    print(f"  ndcg5              {_fmt_delta(before['ndcg5'], after['ndcg5'], '')}")
    print(
        f"  mean_latency_ms    {_fmt_delta(before['mean_latency_ms'], after['mean_latency_ms'], 'ms', bigger_is_better=False)}"
    )
    print(f"  failed_count       {_fmt_delta(before['failed'], after['failed'], '', bigger_is_better=False)}")
    print()

    # Query-level diff
    before_fails = set(before.get("fail_queries", []))
    after_fails = set(after.get("fail_queries", []))
    fixed = before_fails - after_fails
    regressed = after_fails - before_fails
    if fixed:
        print(f"✓ Fixed queries ({len(fixed)}):")
        for q in sorted(fixed):
            print(f"    - {q}")
    if regressed:
        print(f"✗ New failures ({len(regressed)}):")
        for q in sorted(regressed):
            print(f"    - {q}")
    if not fixed and not regressed:
        print("(no per-query changes)")
    print()

    # Verdict
    REGRESSION_THRESHOLD = 2.0
    delta_content = after["hit_content_pct"] - before["hit_content_pct"]
    if delta_content < -REGRESSION_THRESHOLD:
        print(
            f"REGRESSION — content_hit dropped {-delta_content:.2f}pt (>{REGRESSION_THRESHOLD}pt threshold)"
        )
        return 1
    print("PASS — within threshold")
    return 0


def cmd_clear() -> int:
    for p in (SNAP_BEFORE, SNAP_AFTER):
        if p.exists():
            p.unlink()
            print(f"removed {p}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Brain bench regression discipline")
    sub = parser.add_subparsers(dest="cmd", required=True)

    snap = sub.add_parser("snap", help="Snapshot current eval state")
    snap.add_argument("label", choices=["before", "after"])

    sub.add_parser("diff", help="Compare before vs after snapshots")
    sub.add_parser("clear", help="Clear both snapshots")

    args = parser.parse_args()
    if args.cmd == "snap":
        return cmd_snap(args.label)
    if args.cmd == "diff":
        return cmd_diff()
    if args.cmd == "clear":
        return cmd_clear()
    return 2


if __name__ == "__main__":
    sys.exit(main())
