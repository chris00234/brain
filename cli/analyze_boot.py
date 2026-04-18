#!/opt/homebrew/bin/python3
"""Analyze boot context logs to find low-value and stale queries.

Usage:
  analyze_boot.py [--days 14]

Reads boot-context-log.jsonl and reports:
  - Queries with consistently low avg scores
  - Queries returning identical results (stale)
  - Per-agent token cost
"""

import argparse
import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

LOG_FILE = Path("/Users/chrischo/server/brain/logs/boot-context-log.jsonl")


def load_logs(days):
    if not LOG_FILE.exists():
        return []
    cutoff = datetime.now() - timedelta(days=days)
    entries = []
    for line in LOG_FILE.read_text().splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            ts = datetime.fromisoformat(entry["timestamp"])
            if ts >= cutoff:
                entries.append(entry)
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
    return entries


def analyze(entries):
    if not entries:
        print("No boot context logs found. Wait for agents to boot and accumulate data.")
        return

    agent_stats = defaultdict(lambda: {"boots": 0, "total_tokens": 0, "total_results": 0, "scores": []})
    query_scores = defaultdict(list)
    query_sources = defaultdict(list)

    for e in entries:
        agent = e["agent"]
        agent_stats[agent]["boots"] += 1
        agent_stats[agent]["total_tokens"] += e.get("total_tokens", 0)
        agent_stats[agent]["total_results"] += e.get("results_count", 0)
        if e.get("avg_score"):
            agent_stats[agent]["scores"].append(e["avg_score"])

        sources_key = json.dumps(sorted(e.get("sources", [])))
        for q in e.get("queries", []):
            query_scores[f"{agent}:{q}"].append(e.get("avg_score", 0))
            query_sources[f"{agent}:{q}"].append(sources_key)

    print("=" * 60)
    print("BOOT CONTEXT ANALYSIS")
    print(f"Period: last {len(entries)} boots")
    print("=" * 60)

    print("\n## Per-Agent Token Cost")
    for agent, stats in sorted(agent_stats.items()):
        avg_tokens = stats["total_tokens"] / stats["boots"] if stats["boots"] else 0
        avg_score = sum(stats["scores"]) / len(stats["scores"]) if stats["scores"] else 0
        print(
            f"  {agent}: {stats['boots']} boots, avg {avg_tokens:.0f} tokens/boot, avg score {avg_score:.1f}"
        )

    print("\n## Low-Value Queries (avg score < 40)")
    low_value = []
    for qkey, scores in query_scores.items():
        avg = sum(scores) / len(scores) if scores else 0
        if avg < 40 and len(scores) >= 2:
            low_value.append((qkey, avg, len(scores)))
    if low_value:
        for qkey, avg, count in sorted(low_value, key=lambda x: x[1]):
            print(f"  REMOVE? {qkey} — avg score {avg:.1f} over {count} boots")
    else:
        print("  None found (all queries scoring well)")

    print("\n## Stale Queries (identical results every boot)")
    for qkey, source_lists in query_sources.items():
        if len(source_lists) >= 3 and len(set(source_lists)) == 1:
            print(f"  STALE: {qkey} — same {len(source_lists)} times")

    print()


def main():
    parser = argparse.ArgumentParser(description="Boot Context Log Analysis")
    parser.add_argument("--days", type=int, default=14, help="Days to analyze")
    args = parser.parse_args()

    entries = load_logs(args.days)
    analyze(entries)


if __name__ == "__main__":
    main()
