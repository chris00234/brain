"""brain_core/db_maintenance.py — long-term DB health (2026-04-17).

5-year sustainability additions:
  1. VACUUM: SQLite WAL mode doesn't reclaim free pages automatically. After
     many deletes, the file stays large. Weekly VACUUM keeps file size bounded.
  2. action_audit retention: currently ~48K rows with no pruning. Grows by
     every brain_store call. Keep last 90 days for provenance, drop older.
  3. llm_usage retention: already tracks cost per call (2K+ rows in 7 days).
     Keep 90 days of detail, roll up older to monthly aggregates.

All jobs are idempotent, off-hours (Sunday 5:30 or daily 4:20), and log a
summary JSON for observability.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

log = logging.getLogger("brain.db_maintenance")

BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")
AUTONOMY_DB = Path("/Users/chrischo/server/brain/logs/autonomy.db")
LLM_USAGE_DB = Path("/Users/chrischo/server/brain/logs/llm_usage.db")

# Retention policies
ACTION_AUDIT_RETENTION_DAYS = 90
LLM_USAGE_RETENTION_DAYS = 90


def _sqlite_size_mb(path: Path) -> float:
    if not path.exists():
        return 0.0
    return round(path.stat().st_size / 1048576, 2)


def run_vacuum() -> dict:
    """Weekly VACUUM across all brain SQLite DBs. Reclaims free pages, rebuilds
    indexes, keeps file size proportional to live data. Safe with WAL mode but
    briefly serializes writes — run off-hours Sunday 5:30am."""
    summary: dict = {"started_at": _now_iso(), "dbs": []}
    for label, path in [
        ("brain.db", BRAIN_DB),
        ("autonomy.db", AUTONOMY_DB),
        ("llm_usage.db", LLM_USAGE_DB),
    ]:
        if not path.exists():
            continue
        entry = {"label": label, "size_before_mb": _sqlite_size_mb(path)}
        try:
            # SQLite VACUUM requires no active transactions on the connection.
            # Use isolation_level=None for autocommit.
            conn = sqlite3.connect(str(path), isolation_level=None)
            try:
                conn.execute("VACUUM")
                conn.execute("ANALYZE")  # refresh query planner stats
            finally:
                conn.close()
            entry["size_after_mb"] = _sqlite_size_mb(path)
            entry["reclaimed_mb"] = round(entry["size_before_mb"] - entry["size_after_mb"], 2)
            entry["status"] = "ok"
        except Exception as exc:
            entry["status"] = f"error:{str(exc)[:120]}"
        summary["dbs"].append(entry)
    summary["finished_at"] = _now_iso()
    return summary


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def run_action_audit_retention(days: int = ACTION_AUDIT_RETENTION_DAYS) -> dict:
    """Drop action_audit rows older than N days. Provenance for recent activity
    is preserved; older entries are summarized in canonical knowledge if
    relevant. action_audit is the #1 growth source in brain.db (~48K rows already)."""
    summary: dict = {
        "table": "action_audit",
        "days_kept": days,
        "started_at": _now_iso(),
    }
    try:
        conn = sqlite3.connect(str(BRAIN_DB))
        try:
            cur = conn.execute(
                "DELETE FROM action_audit WHERE created_at < datetime('now', 'utc', ? || ' days')",
                (f"-{int(days)}",),
            )
            summary["deleted"] = cur.rowcount
            conn.commit()
            # Keep the same connection to read the count, cheap.
            summary["remaining"] = conn.execute("SELECT count(*) FROM action_audit").fetchone()[0]
            summary["status"] = "ok"
        finally:
            conn.close()
    except Exception as exc:
        summary["status"] = f"error:{str(exc)[:150]}"
    summary["finished_at"] = _now_iso()
    return summary


def run_llm_usage_retention(days: int = LLM_USAGE_RETENTION_DAYS) -> dict:
    """Archive llm_usage rows older than N days into a monthly rollup table.
    Preserves cost analytics (per month) while bounding the detail table size.

    Monthly rollup columns: month (YYYY-MM), agent, total_calls, total_prompt_toks,
    total_response_tokens, total_cost_usd, total_cache_read_tokens.
    """
    summary: dict = {
        "table": "llm_usage",
        "days_kept": days,
        "started_at": _now_iso(),
    }
    if not LLM_USAGE_DB.exists():
        summary["status"] = "db_missing"
        return summary
    try:
        conn = sqlite3.connect(str(LLM_USAGE_DB))
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS llm_usage_monthly (
                    month TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    calls INTEGER NOT NULL DEFAULT 0,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    response_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                    cost_usd REAL NOT NULL DEFAULT 0,
                    rolled_up_at TEXT NOT NULL,
                    PRIMARY KEY (month, agent)
                );
                """
            )
            # Aggregate rows older than retention into the monthly table.
            conn.execute(
                """INSERT INTO llm_usage_monthly
                       (month, agent, calls, prompt_tokens, response_tokens,
                        cache_read_tokens, cost_usd, rolled_up_at)
                   SELECT substr(timestamp, 1, 7) AS month,
                          agent,
                          count(*),
                          COALESCE(SUM(prompt_tokens), 0),
                          COALESCE(SUM(response_tokens), 0),
                          COALESCE(SUM(cache_read_tokens), 0),
                          COALESCE(SUM(cost_usd), 0.0),
                          datetime('now', 'utc')
                   FROM llm_usage
                   WHERE timestamp < datetime('now', 'utc', ? || ' days')
                   GROUP BY month, agent
                   ON CONFLICT(month, agent) DO UPDATE SET
                     calls = excluded.calls,
                     prompt_tokens = excluded.prompt_tokens,
                     response_tokens = excluded.response_tokens,
                     cache_read_tokens = excluded.cache_read_tokens,
                     cost_usd = excluded.cost_usd,
                     rolled_up_at = excluded.rolled_up_at""",
                (f"-{int(days)}",),
            )
            cur = conn.execute(
                "DELETE FROM llm_usage WHERE timestamp < datetime('now', 'utc', ? || ' days')",
                (f"-{int(days)}",),
            )
            summary["archived_rows"] = cur.rowcount
            conn.commit()
            summary["remaining_detail_rows"] = conn.execute("SELECT count(*) FROM llm_usage").fetchone()[0]
            summary["monthly_rows"] = conn.execute("SELECT count(*) FROM llm_usage_monthly").fetchone()[0]
            summary["status"] = "ok"
        finally:
            conn.close()
    except Exception as exc:
        summary["status"] = f"error:{str(exc)[:150]}"
    summary["finished_at"] = _now_iso()
    return summary


def growth_stats() -> dict:
    """One-shot health: row counts + sizes across all brain SQLite DBs.
    Used by /brain/health and growth-rate SLO."""
    stats: dict = {
        "brain_db_mb": _sqlite_size_mb(BRAIN_DB),
        "autonomy_db_mb": _sqlite_size_mb(AUTONOMY_DB),
        "llm_usage_db_mb": _sqlite_size_mb(LLM_USAGE_DB),
        "tables": {},
    }
    for label, path, table in [
        ("atoms", BRAIN_DB, "atoms"),
        ("raw_events", BRAIN_DB, "raw_events"),
        ("action_audit", BRAIN_DB, "action_audit"),
        ("atom_coactivation", BRAIN_DB, "atom_coactivation"),
        ("llm_usage", LLM_USAGE_DB, "llm_usage"),
    ]:
        if not path.exists():
            continue
        try:
            conn = sqlite3.connect(str(path))
            try:
                n = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                stats["tables"][label] = n
            finally:
                conn.close()
        except Exception as _exc:
            log.debug("silenced exception in db_maintenance.py: %s", _exc)
    return stats


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("vacuum")
    sub.add_parser("retention_audit")
    sub.add_parser("retention_usage")
    sub.add_parser("stats")
    args = p.parse_args()
    if args.cmd == "vacuum":
        print(json.dumps(run_vacuum(), indent=2))
    elif args.cmd == "retention_audit":
        print(json.dumps(run_action_audit_retention(), indent=2))
    elif args.cmd == "retention_usage":
        print(json.dumps(run_llm_usage_retention(), indent=2))
    elif args.cmd == "stats":
        print(json.dumps(growth_stats(), indent=2))
    else:
        p.print_help()
