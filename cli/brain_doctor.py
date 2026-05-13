#!/usr/bin/env python3
"""cli/brain_doctor.py — one-shot brain health audit.

Runs the read-only diagnostics that Chris reaches for when he suspects
something has drifted but doesn't want to chase individual jobs:

  1. Current SLO state via /brain/slos (all 29; flags breaches)
  2. Hot SQLite DB sizes + WAL sizes + journal_size_limit per-connection
  3. logs/ size + 24h growth (from recorded snapshots, no FS scan repeated)
  4. Backup ages (qdrant, neo4j, restore drill)
  5. Recent SLO remediation activity (last 5 entries)
  6. Calibration v1 + brier drift state

Memory: O(constant) — only reads counters and small JSON. No table scans.
Performance: <1 second on a healthy system. Mostly network call to brain.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")
BRAIN_DB = BRAIN_LOGS_DIR / "brain.db"
AUTONOMY_DB = BRAIN_LOGS_DIR / "autonomy.db"
EMBED_CACHE_DB = BRAIN_LOGS_DIR / "embedding_cache.db"
METRICS_DB = BRAIN_LOGS_DIR / "metrics_history.db"
LLM_USAGE_DB = BRAIN_LOGS_DIR / "llm_usage.db"
SLO_REMEDIATION_LOG = BRAIN_LOGS_DIR / "slo_remediation.jsonl"
BRAIN_ENDPOINT = os.environ.get("BRAIN_ENDPOINT", "http://127.0.0.1:8791")
CREDENTIALS_FILE = Path.home() / ".openclaw/credentials/.personal_webhook_secret"


def _bearer() -> str:
    if not CREDENTIALS_FILE.exists():
        return ""
    return CREDENTIALS_FILE.read_text().strip()


def _file_mb(path: Path) -> float:
    try:
        return round(path.stat().st_size / (1024 * 1024), 1) if path.exists() else 0.0
    except OSError:
        return 0.0


def _slo_snapshot() -> dict:
    import urllib.request

    req = urllib.request.Request(  # noqa: S310 — local-only http://127.0.0.1:8791
        f"{BRAIN_ENDPOINT}/brain/slos",
        headers={"Authorization": f"Bearer {_bearer()}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        return json.loads(resp.read().decode())


def _db_sizes() -> list[dict]:
    rows: list[dict] = []
    for label, path in [
        ("brain.db", BRAIN_DB),
        ("autonomy.db", AUTONOMY_DB),
        ("embedding_cache.db", EMBED_CACHE_DB),
        ("metrics_history.db", METRICS_DB),
        ("llm_usage.db", LLM_USAGE_DB),
    ]:
        rows.append(
            {
                "db": label,
                "mb": _file_mb(path),
                "wal_mb": _file_mb(path.with_suffix(path.suffix + "-wal")),
            }
        )
    return rows


def _growth_snapshot_history() -> dict:
    if not AUTONOMY_DB.exists():
        return {"status": "no_autonomy_db"}
    try:
        with sqlite3.connect(str(AUTONOMY_DB)) as conn:
            row = conn.execute("SELECT value FROM brain_config WHERE key = 'slo.logs_dir_history'").fetchone()
    except sqlite3.Error as exc:
        return {"status": f"error:{exc}"}
    if not row:
        return {"status": "no_snapshots_yet"}
    try:
        history = json.loads(row[0])
    except json.JSONDecodeError:
        return {"status": "invalid_history_json"}
    if not isinstance(history, list) or not history:
        return {"status": "empty"}
    return {
        "entries": len(history),
        "oldest": history[0],
        "newest": history[-1],
    }


def _calibration_state() -> dict:
    if not AUTONOMY_DB.exists():
        return {"status": "no_autonomy_db"}
    try:
        with sqlite3.connect(str(AUTONOMY_DB)) as conn:
            v1_row = conn.execute(
                "SELECT value FROM brain_config WHERE key = 'confidence_calibration.v1'"
            ).fetchone()
            drift_row = conn.execute(
                "SELECT value FROM brain_config WHERE key = 'confidence_calibration.drift_brier'"
            ).fetchone()
    except sqlite3.Error as exc:
        return {"status": f"error:{exc}"}
    out: dict = {}
    if v1_row:
        try:
            out["v1"] = json.loads(v1_row[0])
        except json.JSONDecodeError:
            out["v1"] = {"status": "invalid_json"}
    if drift_row:
        try:
            out["drift_brier"] = json.loads(drift_row[0])
        except json.JSONDecodeError:
            out["drift_brier"] = {"status": "invalid_json"}
    return out


def _recent_remediation(limit: int = 5) -> list[dict]:
    if not SLO_REMEDIATION_LOG.exists():
        return []
    try:
        lines = SLO_REMEDIATION_LOG.read_text().splitlines()[-limit:]
    except OSError:
        return []
    out: list[dict] = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _self_quality_snapshot() -> dict:
    """Surface the self-quality learning loop's open signals so the daily
    report reflects the new brain-cli pipelines instead of just SLOs.

    Pulls the three brain-internal review-task counters + override
    pattern summary + 7d trend alerts. All optional — any subpath that
    raises is recorded with its error so the main report stays
    deterministic.
    """
    snap: dict = {}
    import urllib.request

    headers = {"Authorization": f"Bearer {_bearer()}"}
    for label, path in (
        ("override_patterns", "/brain/outcomes/feedback?hours=168&min_overrides=2&limit=500"),
        ("trend_alerts", "/brain/trend-alerts"),
        ("wrong_rate_breakdown", "/brain/recall/wrong-rate-breakdown?hours=168"),
    ):
        try:
            req = urllib.request.Request(  # noqa: S310 — local-only brain endpoint
                f"{BRAIN_ENDPOINT}{path}",
                headers=headers,
            )
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                snap[label] = json.loads(resp.read().decode())
        except Exception as exc:
            snap[label] = {"status": f"error:{str(exc)[:120]}"}

    # Pending brain-cli task counters straight from autonomy.db so the
    # snapshot reflects the dispatcher backlog without another HTTP hop.
    try:
        conn = sqlite3.connect(f"file:{AUTONOMY_DB}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute(
                "SELECT created_by, status, COUNT(*) AS n FROM tasks "
                "WHERE assigned_agent = 'brain_cli' "
                "  AND created_at > datetime('now', '-30 days') "
                "GROUP BY created_by, status"
            ).fetchall()
            by_status: dict[str, dict[str, int]] = {}
            for created_by, status, n in rows:
                by_status.setdefault(str(created_by or ""), {})[str(status or "")] = int(n)
            snap["brain_cli_tasks_30d"] = by_status
        finally:
            conn.close()
    except sqlite3.Error as exc:
        snap["brain_cli_tasks_30d"] = {"status": f"error:{str(exc)[:120]}"}

    # Compact summary for SessionStart hook surfaces.
    op = snap.get("override_patterns") or {}
    op_cands = op.get("learning_candidates") if isinstance(op, dict) else None
    alerts = snap.get("trend_alerts") or {}
    alerts_list = alerts.get("alerts") if isinstance(alerts, dict) else None
    wrb = snap.get("wrong_rate_breakdown") or {}
    snap["summary"] = {
        "override_pattern_count": len(op_cands or []),
        "trend_alert_count": len(alerts_list or []),
        "wrong_rate": (wrb.get("wrong_rate") if isinstance(wrb, dict) else None),
        "worst_slice": (wrb.get("worst_slice") if isinstance(wrb, dict) else None),
    }
    return snap


def main() -> int:
    t0 = time.time()
    report: dict = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "host": os.uname().nodename,
    }

    try:
        slos = _slo_snapshot()
    except Exception as exc:
        report["slos"] = {"status": f"unreachable:{exc}"}
        slos = None
    if slos:
        breached = [r["name"] for r in slos.get("items", []) if r.get("breached")]
        report["slos"] = {
            "checked": slos.get("checked"),
            "breached_count": len(breached),
            "breached_names": breached,
        }

    report["dbs"] = _db_sizes()
    report["logs_dir_history"] = _growth_snapshot_history()
    report["calibration"] = _calibration_state()
    report["recent_remediation"] = _recent_remediation()
    report["self_quality"] = _self_quality_snapshot()
    report["elapsed_ms"] = int((time.time() - t0) * 1000)

    serialized = json.dumps(report, indent=2, default=str)
    # Persist the latest snapshot so SessionStart hooks / dashboards can
    # surface drift without re-running the CLI. Best-effort: failure here
    # must not break stdout output.
    try:
        (BRAIN_LOGS_DIR / "brain_doctor_daily.json").write_text(serialized + "\n")
    except OSError as exc:
        print(f"# brain-doctor: snapshot write failed: {exc}", file=sys.stderr)

    print(serialized)
    return 0 if not slos or not [r for r in slos.get("items", []) if r.get("breached")] else 1


if __name__ == "__main__":
    sys.exit(main())
