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

try:
    from config import AUTONOMY_DB, BRAIN_DB, BRAIN_LOGS_DIR

    LLM_USAGE_DB = BRAIN_LOGS_DIR / "llm_usage.db"
    METRICS_HISTORY_DB = BRAIN_LOGS_DIR / "metrics_history.db"
except ImportError:
    BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")
    AUTONOMY_DB = Path("/Users/chrischo/server/brain/logs/autonomy.db")
    LLM_USAGE_DB = Path("/Users/chrischo/server/brain/logs/llm_usage.db")
    METRICS_HISTORY_DB = Path("/Users/chrischo/server/brain/logs/metrics_history.db")

# Retention policies
ACTION_AUDIT_RETENTION_DAYS = 90
LLM_USAGE_RETENTION_DAYS = 90
# raw_events is the ingest log for atom synthesis. Most rows get referenced
# by atoms.raw_event_id; many do not (every active source still writes a
# `raw_events` row whether or not an atom lands). The `processed_at` column
# is dead schema — no code path sets it — so retention can't rely on that.
# Instead we prune rows that are old AND unreferenced AND not from sources
# we know maintain their own sidecar reference (coding_event_outcomes,
# atoms_hot_path internal provenance). 14d gives the synthesis pipeline
# two weeks to claim a row before pruning; longer windows leave legacy
# unreferenced rows accumulating since they will never be claimed.
RAW_EVENTS_RETENTION_DAYS = 14
RAW_EVENTS_PROTECTED_SOURCES = ("coding_event", "atoms_hot_path")
# autonomy_decisions writes ~48K rows/day (one per autonomy.authorize call).
# Only db_maintenance and incident review read it; nothing on the hot path.
# 14d window keeps two weeks of gate-check audit, steady-state ~670K rows.
# Without retention the table grew 600KB → 81MB in 8 days.
AUTONOMY_DECISIONS_RETENTION_DAYS = 14
# slos.py only reads the most-recent 20 rows of metrics_snapshots; everything
# older is observability history. 14d keeps two weeks of trend; the existing
# 90d DELETE in metrics_buffer.persist remains as the longer-term safety net.
METRICS_HISTORY_RETENTION_DAYS = 14
# session_context cleanup is normally session-scoped via wm_consolidate on
# SessionEnd. Crashes / never-ended sessions accumulate orphans. 30d sweep
# catches them without losing live working memory of active sessions.
SESSION_CONTEXT_RETENTION_DAYS = 30
# Auto-obsolete atoms whose valid_until passed N days ago AND who have a
# proper superseded_by chain AND were never reinforced. Conservative window:
# we trust the supersession chain (set by ingest_mirror's semantic gate or
# explicit AI replaces=); we don't blanket-obsolete every expired atom.
EXPIRED_ATOM_OBSOLETE_DAYS = 60


def _sqlite_size_mb(path: Path) -> float:
    if not path.exists():
        return 0.0
    return round(path.stat().st_size / 1048576, 2)


# WAL ceiling per hot DB (bytes). SQLite's `journal_size_limit` is a
# per-connection setting — it does NOT persist in the database file header —
# so to actually bound the WAL during the day every long-lived connection on
# hot DBs has to call `apply_hot_db_pragmas` after enabling WAL mode. Daily
# checkpoint truncates to 0; the in-day ceiling caps growth between cycles.
# 96 MiB across 5 hot DBs caps the WAL contribution at ~480 MiB total,
# leaving headroom inside the 3 GiB logs_dir budget.
WAL_JOURNAL_SIZE_LIMIT_BYTES = 96 * 1024 * 1024


LOGS_DIR_HISTORY_KEY = "slo.logs_dir_history"
LOGS_DIR_HISTORY_MAX_ENTRIES = 14  # ~2 weeks of daily snapshots


def _logs_dir_total_mb() -> float:
    """Sum of all file sizes under BRAIN_LOGS_DIR in MB. O(directory tree)."""
    total = 0
    for p in BRAIN_LOGS_DIR.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                continue
    return round(total / (1024 * 1024), 1)


def record_logs_dir_size_snapshot() -> dict:
    """Append the current logs-dir size to brain_config_store history.

    Stores a bounded JSON list of `{ts, mb}` snapshots (max 14 entries),
    used by `logs_dir_growth_24h_mb` SLO to detect anomalous daily growth.
    O(1) memory: list never exceeds 14 entries.
    """
    summary: dict = {"started_at": _now_iso()}
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import brain_config_store

        mb = _logs_dir_total_mb()
        raw = brain_config_store.get(LOGS_DIR_HISTORY_KEY) or "[]"
        try:
            history = json.loads(raw)
            if not isinstance(history, list):
                history = []
        except json.JSONDecodeError:
            history = []
        history.append({"ts": _now_iso(), "mb": mb})
        history = history[-LOGS_DIR_HISTORY_MAX_ENTRIES:]
        brain_config_store.set(
            LOGS_DIR_HISTORY_KEY,
            json.dumps(history, separators=(",", ":")),
            updated_by="db_maintenance.record_logs_dir_size_snapshot",
        )
        summary["mb"] = mb
        summary["entries"] = len(history)
        summary["status"] = "ok"
    except Exception as exc:
        summary["status"] = f"error:{str(exc)[:120]}"
    summary["finished_at"] = _now_iso()
    return summary


def apply_hot_db_pragmas(conn: sqlite3.Connection) -> None:
    """Apply the standard WAL ceiling on a hot-DB connection.

    Called by every long-lived brain-server connection after the existing
    `PRAGMA journal_mode=WAL`. Without this, the WAL file balloons to
    200-300 MB between daily TRUNCATE cycles because SQLite has no
    per-commit ceiling unless this is set on the writing connection.
    """
    try:
        conn.execute(f"PRAGMA journal_size_limit = {WAL_JOURNAL_SIZE_LIMIT_BYTES}").fetchone()
    except sqlite3.Error as exc:
        log.debug("apply_hot_db_pragmas: journal_size_limit skipped: %s", exc)


_HOT_DBS: tuple[tuple[str, Path], ...] = (
    ("brain.db", BRAIN_DB),
    ("autonomy.db", AUTONOMY_DB),
    ("llm_usage.db", LLM_USAGE_DB),
    ("metrics_history.db", METRICS_HISTORY_DB),
    ("embedding_cache.db", BRAIN_LOGS_DIR / "embedding_cache.db"),
)


def run_wal_checkpoint() -> dict:
    """Daily PRAGMA wal_checkpoint(TRUNCATE) across hot brain SQLite DBs.

    Why: weekly VACUUM implicitly checkpoints, but between vacuums the WAL
    grows unbounded under steady writes (autonomy.authorize ~48K/day,
    embedding_cache inserts, metrics_snapshots persist). Observed
    2026-04-30: embedding_cache.db-wal at 224 MB and autonomy.db-wal at
    176 MB triggered the logs_dir_total_mb SLO. Daily TRUNCATE keeps WAL
    bounded to one day of writes.

    TRUNCATE is safe with active readers/writers — falls back to PASSIVE
    when blocked rather than waiting indefinitely.

    Long-term durability (2026-05-11): even with the daily TRUNCATE the
    brain server's long-running connections keep blocking truncation
    because new writes land between this job and the next, so the WAL
    file balloons back to 200-300 MB by midday. We now also set
    `PRAGMA journal_size_limit` here AND on each hot writer's connection
    (see `apply_hot_db_pragmas`). The limit is per-connection — SQLite
    does not persist it in the file header — so every long-lived hot-DB
    connection must call the helper after enabling WAL mode, or the
    in-day WAL ceiling regresses.
    """
    summary: dict = {"started_at": _now_iso(), "dbs": []}
    for label, path in _HOT_DBS:
        if not path.exists():
            continue
        wal = path.with_suffix(path.suffix + "-wal")
        entry = {
            "label": label,
            "wal_size_before_mb": _sqlite_size_mb(wal),
        }
        try:
            conn = sqlite3.connect(str(path), isolation_level=None, timeout=30.0)
            try:
                conn.execute(f"PRAGMA journal_size_limit = {WAL_JOURNAL_SIZE_LIMIT_BYTES}").fetchone()
                row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                entry["result"] = list(row) if row else None
            finally:
                conn.close()
            entry["wal_size_after_mb"] = _sqlite_size_mb(wal)
            entry["status"] = "ok"
        except Exception as exc:
            entry["status"] = f"error:{str(exc)[:120]}"
        summary["dbs"].append(entry)
    # Daily logs_dir size snapshot for growth-rate SLO. Runs after the WAL
    # truncate so the snapshot reflects the post-checkpoint steady state,
    # not the mid-day WAL bulge.
    summary["logs_dir_snapshot"] = record_logs_dir_size_snapshot()
    summary["finished_at"] = _now_iso()
    return summary


def run_vacuum() -> dict:
    """Weekly VACUUM across all brain SQLite DBs. Reclaims free pages, rebuilds
    indexes, keeps file size proportional to live data. Safe with WAL mode but
    briefly serializes writes — run off-hours Sunday 5:30am."""
    summary: dict = {"started_at": _now_iso(), "dbs": []}
    for label, path in [
        ("brain.db", BRAIN_DB),
        ("autonomy.db", AUTONOMY_DB),
        ("llm_usage.db", LLM_USAGE_DB),
        ("metrics_history.db", METRICS_HISTORY_DB),
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


def run_raw_events_retention(days: int = RAW_EVENTS_RETENTION_DAYS) -> dict:
    """Drop unreferenced raw_events older than N days, protecting sources that
    maintain their own sidecar links.

    raw_events accumulated ~10k stale rows once the multi-source ingest pipeline
    shifted to source-aware atom synthesis. Most legacy openclaw_session /
    browser / claude_code_session rows now sit unreferenced because no atom
    points back at them. The protected source list keeps coding_event
    (referenced by coding_event_outcomes.event_id) and atoms_hot_path (internal
    provenance still being written by upsert_atom).

    The FTS shadow table `raw_events_fts` syncs via AFTER DELETE triggers,
    so deleting from raw_events auto-cleans the index. No manual FTS work.
    """
    summary: dict = {
        "table": "raw_events",
        "days_kept": days,
        "protected_sources": list(RAW_EVENTS_PROTECTED_SOURCES),
        "started_at": _now_iso(),
    }
    try:
        conn = sqlite3.connect(str(BRAIN_DB))
        try:
            placeholders = ",".join("?" for _ in RAW_EVENTS_PROTECTED_SOURCES)
            cur = conn.execute(
                f"DELETE FROM raw_events "
                f"WHERE created_at < datetime('now', 'utc', ? || ' days') "
                f"  AND source_type NOT IN ({placeholders}) "
                f"  AND id NOT IN (SELECT raw_event_id FROM atoms WHERE raw_event_id IS NOT NULL)",
                (f"-{int(days)}", *RAW_EVENTS_PROTECTED_SOURCES),
            )
            summary["deleted"] = cur.rowcount
            conn.commit()
            summary["remaining"] = conn.execute("SELECT count(*) FROM raw_events").fetchone()[0]
            summary["status"] = "ok"
        finally:
            conn.close()
    except Exception as exc:
        summary["status"] = f"error:{str(exc)[:150]}"
    summary["finished_at"] = _now_iso()
    return summary


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


def run_autonomy_decisions_retention(days: int = AUTONOMY_DECISIONS_RETENTION_DAYS) -> dict:
    """Drop autonomy_decisions rows older than N days. The table records every
    autonomy.authorize gate check (~48K rows/day) and was previously unbounded —
    autonomy.db grew 600KB → 86MB in 8 days before retention.

    decision_ledger (full decision units) and outcomes (Chris-override
    feedback) are intentionally retained longer; this only prunes the gate-
    audit trail.
    """
    summary: dict = {
        "table": "autonomy_decisions",
        "days_kept": days,
        "started_at": _now_iso(),
    }
    if not AUTONOMY_DB.exists():
        summary["status"] = "db_missing"
        summary["finished_at"] = _now_iso()
        return summary
    try:
        conn = sqlite3.connect(str(AUTONOMY_DB))
        try:
            cur = conn.execute(
                "DELETE FROM autonomy_decisions WHERE ts_utc < datetime('now', 'utc', ? || ' days')",
                (f"-{int(days)}",),
            )
            summary["deleted"] = cur.rowcount
            conn.commit()
            summary["remaining"] = conn.execute("SELECT count(*) FROM autonomy_decisions").fetchone()[0]
            summary["status"] = "ok"
        finally:
            conn.close()
    except Exception as exc:
        summary["status"] = f"error:{str(exc)[:150]}"
    summary["finished_at"] = _now_iso()
    return summary


def run_session_context_retention(days: int = SESSION_CONTEXT_RETENTION_DAYS) -> dict:
    """Sweep session_context rows older than N days. Normally
    wm_consolidate clears a session's rows on SessionEnd; this catches
    orphans from crashes or sessions that never explicitly ended.
    """
    summary: dict = {
        "table": "session_context",
        "days_kept": days,
        "started_at": _now_iso(),
    }
    if not AUTONOMY_DB.exists():
        summary["status"] = "db_missing"
        summary["finished_at"] = _now_iso()
        return summary
    try:
        conn = sqlite3.connect(str(AUTONOMY_DB))
        try:
            cur = conn.execute(
                "DELETE FROM session_context WHERE updated_at < datetime('now', 'utc', ? || ' days')",
                (f"-{int(days)}",),
            )
            summary["deleted"] = cur.rowcount
            conn.commit()
            summary["remaining"] = conn.execute("SELECT count(*) FROM session_context").fetchone()[0]
            summary["status"] = "ok"
        finally:
            conn.close()
    except Exception as exc:
        summary["status"] = f"error:{str(exc)[:150]}"
    summary["finished_at"] = _now_iso()
    return summary


def run_obsolete_expired_atoms(days: int = EXPIRED_ATOM_OBSOLETE_DAYS) -> dict:
    """Auto-mark very-stale atoms `tier='obsolete'` so they stop appearing
    in retrieval entirely.

    Only targets atoms that meet ALL of:
      - valid_until is set AND older than `days` (default 60d)
      - superseded_by is set (the supersession chain explicitly recorded
        a replacement — either via AI explicit replaces= or via the
        cosine gate in ingest_mirror)
      - reinforcement_count == 0 (never accessed since being expired)
      - tier != 'obsolete' already

    Atoms that are merely expired but lack a supersede chain are LEFT
    ALONE — the underlying fact may still be true (Chris's pushback in
    the 2026-04-26 stale audit). time_decay's 0.3x ranking penalty
    continues to apply to them.

    Safety: max 50 per run, max 5% of active atoms.
    """
    summary: dict = {
        "table": "atoms",
        "days": days,
        "started_at": _now_iso(),
        "obsoleted": [],
        "skipped_safety_cap": False,
    }
    if not BRAIN_DB.exists():
        summary["status"] = "db_missing"
        summary["finished_at"] = _now_iso()
        return summary
    try:
        conn = sqlite3.connect(str(BRAIN_DB))
        conn.row_factory = sqlite3.Row
        try:
            total_active = conn.execute("SELECT COUNT(*) FROM atoms WHERE tier != 'obsolete'").fetchone()[0]
            safety_cap = min(50, max(1, int(total_active * 0.05)))
            cur = conn.execute(
                "SELECT id, kind, substr(text, 1, 80) AS preview "
                "FROM atoms "
                "WHERE tier != 'obsolete' "
                "AND superseded_by IS NOT NULL "
                "AND valid_until IS NOT NULL "
                "AND valid_until < datetime('now', 'utc', ? || ' days') "
                "AND reinforcement_count = 0 "
                "ORDER BY valid_until ASC "
                "LIMIT ?",
                (f"-{int(days)}", safety_cap + 1),
            )
            candidates = [dict(row) for row in cur.fetchall()]
            if len(candidates) > safety_cap:
                summary["skipped_safety_cap"] = True
                summary["candidates"] = len(candidates)
                summary["safety_cap"] = safety_cap
                summary["status"] = "skipped:exceeds_cap"
                summary["finished_at"] = _now_iso()
                return summary
            if not candidates:
                summary["status"] = "ok"
                summary["finished_at"] = _now_iso()
                return summary
            now_iso = _now_iso()
            ids = [c["id"] for c in candidates]
            placeholders = ",".join("?" for _ in ids)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                f"UPDATE atoms SET tier = 'obsolete', updated_at = ? WHERE id IN ({placeholders})",
                [now_iso, *ids],
            )
            conn.commit()
            summary["obsoleted"] = ids
            summary["count"] = len(ids)
            summary["status"] = "ok"
        finally:
            conn.close()
    except Exception as exc:
        summary["status"] = f"error:{str(exc)[:150]}"
    summary["finished_at"] = _now_iso()
    # Best-effort audit log so the auto-obsolete decisions are visible.
    if summary.get("obsoleted"):
        try:
            from audit_log import log_event

            for atom_id in summary["obsoleted"]:
                log_event(
                    event_type="atom_obsolete",
                    source="db_maintenance.run_obsolete_expired_atoms",
                    payload={"atom_id": atom_id, "reason": f"expired_>{days}d_with_supersede_chain"},
                )
        except Exception as exc:
            log.debug("run_obsolete_expired_atoms audit log skipped: %s", exc)
    return summary


def run_metrics_history_retention(days: int = METRICS_HISTORY_RETENTION_DAYS) -> dict:
    """Trim metrics_snapshots beyond N days. metrics_buffer.persist already
    does a 90d DELETE on every persist, but the file does not shrink without
    a VACUUM. This job is the safety net that DELETEs aggressively (30d) so
    the weekly VACUUM has reclaimable pages.
    """
    summary: dict = {
        "table": "metrics_snapshots",
        "days_kept": days,
        "started_at": _now_iso(),
    }
    if not METRICS_HISTORY_DB.exists():
        summary["status"] = "db_missing"
        summary["finished_at"] = _now_iso()
        return summary
    try:
        conn = sqlite3.connect(str(METRICS_HISTORY_DB))
        try:
            cur = conn.execute(
                "DELETE FROM metrics_snapshots WHERE timestamp < datetime('now', 'utc', ? || ' days')",
                (f"-{int(days)}",),
            )
            summary["deleted"] = cur.rowcount
            conn.commit()
            summary["remaining"] = conn.execute("SELECT count(*) FROM metrics_snapshots").fetchone()[0]
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
        "metrics_history_db_mb": _sqlite_size_mb(METRICS_HISTORY_DB),
        "tables": {},
    }
    for label, path, table in [
        ("atoms", BRAIN_DB, "atoms"),
        ("raw_events", BRAIN_DB, "raw_events"),
        ("action_audit", BRAIN_DB, "action_audit"),
        ("atom_coactivation", BRAIN_DB, "atom_coactivation"),
        ("autonomy_decisions", AUTONOMY_DB, "autonomy_decisions"),
        ("decision_ledger", AUTONOMY_DB, "decision_ledger"),
        ("metrics_snapshots", METRICS_HISTORY_DB, "metrics_snapshots"),
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
    sub.add_parser("wal_checkpoint")
    sub.add_parser("retention_audit")
    sub.add_parser("retention_raw_events")
    sub.add_parser("retention_usage")
    sub.add_parser("retention_decisions")
    sub.add_parser("retention_metrics")
    sub.add_parser("retention_session_context")
    sub.add_parser("obsolete_expired_atoms")
    sub.add_parser("stats")
    args = p.parse_args()
    if args.cmd == "vacuum":
        print(json.dumps(run_vacuum(), indent=2))
    elif args.cmd == "wal_checkpoint":
        print(json.dumps(run_wal_checkpoint(), indent=2))
    elif args.cmd == "retention_audit":
        print(json.dumps(run_action_audit_retention(), indent=2))
    elif args.cmd == "retention_raw_events":
        print(json.dumps(run_raw_events_retention(), indent=2))
    elif args.cmd == "retention_usage":
        print(json.dumps(run_llm_usage_retention(), indent=2))
    elif args.cmd == "retention_decisions":
        print(json.dumps(run_autonomy_decisions_retention(), indent=2))
    elif args.cmd == "retention_metrics":
        print(json.dumps(run_metrics_history_retention(), indent=2))
    elif args.cmd == "retention_session_context":
        print(json.dumps(run_session_context_retention(), indent=2))
    elif args.cmd == "obsolete_expired_atoms":
        print(json.dumps(run_obsolete_expired_atoms(), indent=2))
    elif args.cmd == "stats":
        print(json.dumps(growth_stats(), indent=2))
    else:
        p.print_help()
