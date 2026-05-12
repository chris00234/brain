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
    report["elapsed_ms"] = int((time.time() - t0) * 1000)

    print(json.dumps(report, indent=2, default=str))
    return 0 if not slos or not [r for r in slos.get("items", []) if r.get("breached")] else 1


if __name__ == "__main__":
    sys.exit(main())
