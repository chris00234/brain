#!/usr/bin/env python3
"""Per-agent active brain usage stats.

Surfaces the gap Chris flagged 2026-05-15: agents underuse brain. Cross-agent
visibility creates accountability. Counts (last 24h and 7d) per agent:
  - atoms stored
  - outcomes (recommendations evaluated)
  - chris_override rate
  - dominant store sources

Run as CLI for ad-hoc inspection. Add to brain scheduler for daily morning
briefing inclusion. UI panel in brain-ui is a separate follow-up.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")
AUTONOMY_DB = Path("/Users/chrischo/server/brain/logs/autonomy.db")


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _normalize_agent(name: str | None) -> str:
    """Drop the 'agent:' prefix used by SessionEnd distill so claude-code,
    agent:claude-code, and agent:claude_code collapse to a single bucket.
    """
    if not name:
        return "(unknown)"
    n = name.lower()
    if n.startswith("agent:"):
        n = n[6:]
    n = n.replace("_", "-")
    return n


def _store_counts(brain: sqlite3.Connection, window: str) -> dict[str, int]:
    rows = brain.execute(
        f"""
        SELECT json_extract(provenance_json, '$.agent') AS agent, COUNT(*) AS cnt
          FROM atoms
         WHERE created_at > datetime('now', '{window}')
         GROUP BY agent
        """
    ).fetchall()
    out: dict[str, int] = {}
    for r in rows:
        a = _normalize_agent(r["agent"])
        out[a] = out.get(a, 0) + r["cnt"]
    return out


def _outcome_stats(auto: sqlite3.Connection, window: str) -> dict[str, dict]:
    """Outcomes don't carry agent attribution directly — use domain as proxy
    where the domain maps to a typical agent (infra→ellie, coding→liz, etc).
    For now just emit by domain.
    """
    rows = auto.execute(
        f"""
        SELECT domain,
               COUNT(*) AS total,
               SUM(chris_override) AS overrides
          FROM outcomes
         WHERE created_at > datetime('now', '{window}')
         GROUP BY domain
        """
    ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        total = r["total"] or 0
        overrides = r["overrides"] or 0
        out[r["domain"]] = {
            "outcomes": total,
            "overrides": overrides,
            "override_rate": round(overrides / total, 3) if total else 0.0,
        }
    return out


def run() -> dict:
    if not BRAIN_DB.exists() or not AUTONOMY_DB.exists():
        return {"error": "missing db"}

    brain = _connect(BRAIN_DB)
    auto = _connect(AUTONOMY_DB)
    try:
        stores_24h = _store_counts(brain, "-1 day")
        stores_7d = _store_counts(brain, "-7 days")
        stores_30d = _store_counts(brain, "-30 days")
        outcomes_7d = _outcome_stats(auto, "-7 days")
    finally:
        brain.close()
        auto.close()

    # Cross-agent summary table — focus on agents Chris uses interactively.
    tracked = ["claude-code", "claude", "codex", "jenna", "liz", "ellie", "sage", "market"]
    agents: list[dict] = []
    for name in tracked:
        agents.append(
            {
                "agent": name,
                "stores_24h": stores_24h.get(name, 0),
                "stores_7d": stores_7d.get(name, 0),
                "stores_30d": stores_30d.get(name, 0),
            }
        )

    # Sort by 7d activity (descending) so under-active agents are obvious.
    agents.sort(key=lambda x: x["stores_7d"], reverse=True)

    return {
        "generated_at_utc": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "agents": agents,
        "outcomes_by_domain_7d": outcomes_7d,
        "interpretation": {
            "passive_agents_7d": [a["agent"] for a in agents if a["stores_7d"] < 5],
            "high_override_domains": [
                d for d, s in outcomes_7d.items() if s["override_rate"] >= 0.5 and s["outcomes"] >= 5
            ],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--format", choices=["json", "text"], default="text")
    args = parser.parse_args()
    result = run()

    if args.format == "json":
        print(json.dumps(result, indent=2))
        return 0

    print(f"Brain agent activity — {result.get('generated_at_utc','?')}")
    print("=" * 60)
    print(f"{'Agent':<14} {'24h':>5} {'7d':>5} {'30d':>5}")
    print("-" * 60)
    for a in result.get("agents", []):
        print(f"{a['agent']:<14} {a['stores_24h']:>5} {a['stores_7d']:>5} {a['stores_30d']:>5}")
    print()
    passive = result.get("interpretation", {}).get("passive_agents_7d", [])
    if passive:
        print(f"PASSIVE (< 5 stores in 7d): {', '.join(passive)}")
    high_or = result.get("interpretation", {}).get("high_override_domains", [])
    if high_or:
        print(f"HIGH OVERRIDE (≥ 50%): {', '.join(high_or)}")
    print()
    print("Outcomes by domain (7d):")
    for d, s in (result.get("outcomes_by_domain_7d") or {}).items():
        print(
            f"  {d:<24} outcomes={s['outcomes']:>4} overrides={s['overrides']:>4} rate={s['override_rate']:.2f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
