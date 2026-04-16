#!/usr/bin/env python3
"""brain_cost_cli.py — LLM dispatch usage summary (subscription-aware).

Reads logs/llm_usage.db and prints call volume + token throughput + rate
limit headroom. Chris is on a flat-rate subscription, so per-token retail
$ are NOT what he actually pays — this CLI tracks what IS meaningful on
a subscription plan:

  - Call volume (5-hour + weekly message limits)
  - Token throughput (for latency + cache efficiency insight)
  - Per-agent / per-model distribution (to spot misrouting)
  - Implied retail cost (for reference only, NOT a budget gate)

Usage:
  brain cost today         # today's call volume + token throughput
  brain cost week          # last 7 days
  brain cost agent         # per-agent breakdown for last 24h
  brain cost model         # per-provider/model breakdown
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

LLM_USAGE_DB = Path("/Users/chrischo/server/brain/logs/llm_usage.db")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(LLM_USAGE_DB), timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _today_cutoff() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _ago_cutoff(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).isoformat()


def cmd_today(args: argparse.Namespace) -> int:
    cutoff = _today_cutoff()
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS calls, "
            "       COALESCE(SUM(prompt_tokens),0) AS in_tok, "
            "       COALESCE(SUM(response_tokens),0) AS out_tok, "
            "       COALESCE(SUM(cache_read_tokens),0) AS cache_tok, "
            "       COALESCE(SUM(cost_usd),0) AS cost, "
            "       COALESCE(SUM(duration_ms),0)/1000.0 AS total_seconds "
            "FROM llm_usage WHERE timestamp >= ? AND ok = 1",
            (cutoff,),
        ).fetchone()
        metered = conn.execute(
            "SELECT COUNT(*) FROM llm_usage WHERE timestamp >= ? AND cost_usd > 0",
            (cutoff,),
        ).fetchone()[0]

    print(f"=== LLM usage today ({cutoff}) — subscription-aware ===")
    print(f"total calls:       {row['calls']}")
    print(f"metered calls:     {metered}  ({'some calls pre-metering-fix' if metered < row['calls'] else 'all metered'})")
    print(f"input tokens:      {row['in_tok']:>12,}")
    print(f"output tokens:     {row['out_tok']:>12,}")
    print(f"cache read tokens: {row['cache_tok']:>12,}  (subscription: counts toward quota at reduced rate)")
    print(f"total duration:    {row['total_seconds']:.0f}s  ({row['total_seconds']/3600:.1f}h)")
    if metered:
        print(f"retail-$ estimate: ${row['cost']:.2f}  (REFERENCE ONLY — not what Chris pays on subscription)")
        print(f"avg per call:      ${row['cost']/metered:.4f}")
    if row["calls"] >= 200:
        print(f"⚠ High call volume — check OpenClaw rate-limit headroom before bulk work")
    return 0


def cmd_week(args: argparse.Namespace) -> int:
    cutoff = _ago_cutoff(7)
    with _conn() as conn:
        rows = conn.execute(
            "SELECT substr(timestamp, 1, 10) AS day, "
            "       COUNT(*) AS calls, "
            "       COALESCE(SUM(cost_usd),0) AS cost, "
            "       COALESCE(SUM(prompt_tokens + response_tokens),0) AS tokens "
            "FROM llm_usage WHERE timestamp >= ? AND ok = 1 "
            "GROUP BY day ORDER BY day DESC",
            (cutoff,),
        ).fetchall()
    print("=== LLM cost last 7 days ===")
    print(f"{'day':<12} {'calls':>6} {'tokens':>12} {'cost':>12}")
    total_cost = 0.0
    for r in rows:
        total_cost += r["cost"]
        print(f"{r['day']:<12} {r['calls']:>6} {r['tokens']:>12,} ${r['cost']:>10.4f}")
    print(f"{'TOTAL':<12} {'':<6} {'':<12} ${total_cost:>10.4f}")
    return 0


def cmd_agent(args: argparse.Namespace) -> int:
    cutoff = _ago_cutoff(1)
    with _conn() as conn:
        rows = conn.execute(
            "SELECT agent, COUNT(*) AS calls, "
            "       COALESCE(SUM(prompt_tokens),0) AS in_tok, "
            "       COALESCE(SUM(response_tokens),0) AS out_tok, "
            "       COALESCE(SUM(cache_read_tokens),0) AS cache_tok, "
            "       COALESCE(SUM(cost_usd),0) AS cost, "
            "       COALESCE(AVG(duration_ms),0) AS avg_ms "
            "FROM llm_usage WHERE timestamp >= ? AND ok = 1 "
            "GROUP BY agent ORDER BY cost DESC",
            (cutoff,),
        ).fetchall()
    print("=== Per-agent breakdown (last 24h) ===")
    print(f"{'agent':<12} {'calls':>6} {'in_tok':>10} {'out_tok':>8} {'cache':>10} {'avg_ms':>8} {'cost':>10}")
    total_cost = 0.0
    for r in rows:
        total_cost += r["cost"]
        print(f"{r['agent']:<12} {r['calls']:>6} {r['in_tok']:>10,} {r['out_tok']:>8,} "
              f"{r['cache_tok']:>10,} {r['avg_ms']:>8.0f} ${r['cost']:>8.4f}")
    print(f"{'TOTAL':<12} {'':>6} {'':>10} {'':>8} {'':>10} {'':>8} ${total_cost:>8.4f}")
    return 0


def cmd_model(args: argparse.Namespace) -> int:
    cutoff = _ago_cutoff(1)
    with _conn() as conn:
        rows = conn.execute(
            "SELECT provider, model, COUNT(*) AS calls, "
            "       COALESCE(SUM(prompt_tokens),0) AS in_tok, "
            "       COALESCE(SUM(response_tokens),0) AS out_tok, "
            "       COALESCE(SUM(cost_usd),0) AS cost "
            "FROM llm_usage WHERE timestamp >= ? AND ok = 1 "
            "GROUP BY provider, model ORDER BY cost DESC",
            (cutoff,),
        ).fetchall()
    print("=== Per-model breakdown (last 24h) ===")
    print(f"{'provider':<15} {'model':<25} {'calls':>6} {'in_tok':>12} {'out_tok':>10} {'cost':>12}")
    total_cost = 0.0
    for r in rows:
        total_cost += r["cost"]
        provider = r["provider"] or "(unknown)"
        model = r["model"] or "(unknown)"
        print(f"{provider:<15} {model:<25} {r['calls']:>6} {r['in_tok']:>12,} {r['out_tok']:>10,} ${r['cost']:>10.4f}")
    print(f"{'TOTAL':<15} {'':<25} {'':<6} {'':<12} {'':<10} ${total_cost:>10.4f}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="brain_cost_cli", description="LLM dispatch cost summary.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("today").set_defaults(func=cmd_today)
    sub.add_parser("week").set_defaults(func=cmd_week)
    sub.add_parser("agent").set_defaults(func=cmd_agent)
    sub.add_parser("model").set_defaults(func=cmd_model)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
