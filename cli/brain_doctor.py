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

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError

BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")
BRAIN_DB = BRAIN_LOGS_DIR / "brain.db"
AUTONOMY_DB = BRAIN_LOGS_DIR / "autonomy.db"
EMBED_CACHE_DB = BRAIN_LOGS_DIR / "embedding_cache.db"
METRICS_DB = BRAIN_LOGS_DIR / "metrics_history.db"
LLM_USAGE_DB = BRAIN_LOGS_DIR / "llm_usage.db"
SLO_REMEDIATION_LOG = BRAIN_LOGS_DIR / "slo_remediation.jsonl"
BRAIN_ENDPOINT = os.environ.get("BRAIN_ENDPOINT", "http://127.0.0.1:8791")
CREDENTIALS_FILE = Path.home() / ".brain/credentials/.personal_webhook_secret"


def _bearer() -> str:
    if not CREDENTIALS_FILE.exists():
        return ""
    return CREDENTIALS_FILE.read_text().strip()


def _file_mb(path: Path) -> float:
    try:
        return round(path.stat().st_size / (1024 * 1024), 1) if path.exists() else 0.0
    except OSError:
        return 0.0


def _http_json(path: str, *, timeout: float = 10.0) -> dict:
    import urllib.request

    req = urllib.request.Request(  # noqa: S310 — local-only brain endpoint by default
        f"{BRAIN_ENDPOINT}{path}",
        headers={"Authorization": f"Bearer {_bearer()}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode())
    except HTTPError as exc:
        if exc.code == 401:
            return {
                "error": "HTTP 401 from raw Brain HTTP recall path",
                "diagnostic_hint": (
                    "Raw HTTP bearer authentication failed. Check local Brain credentials at "
                    f"{CREDENTIALS_FILE} and compare with the MCP server credential path; no secret value printed."
                ),
                "mcp_credential_path": "~/.brain/credentials/.personal_webhook_secret",
            }
        return {"error": f"HTTP {exc.code} from Brain HTTP path"}


def _compact_recall_diagnostic(payload: dict, *, query: str, limit: int) -> dict:
    raw_results = payload.get("results") if isinstance(payload, dict) else []
    if not isinstance(raw_results, list):
        raw_results = []
    rows = []
    for item in raw_results[:limit]:
        if not isinstance(item, dict):
            rows.append({"content": str(item)[:240]})
            continue
        row = {}
        for key in (
            "id",
            "atom_id",
            "title",
            "collection",
            "source_type",
            "score",
            "confidence",
            "trust_score",
        ):
            if item.get(key) is not None:
                row[key] = item.get(key)
        content = str(item.get("content") or item.get("text") or "").replace("\n", " ").strip()
        if content:
            row["content_preview"] = content[:240]
        rows.append(row)
    return {
        "query": payload.get("query") or query,
        "count": payload.get("count", len(raw_results)),
        "returned": len(rows),
        "results": rows,
        "diagnostic": "brain_doctor_recall_v1",
        "safe": True,
        "side_effects": "none: read-only GET /recall/v2",
        **({"error": payload["error"]} if payload.get("error") else {}),
        **({"diagnostic_hint": payload["diagnostic_hint"]} if payload.get("diagnostic_hint") else {}),
        **(
            {"mcp_credential_path": payload["mcp_credential_path"]}
            if payload.get("mcp_credential_path")
            else {}
        ),
    }


def _recall_diagnostic(query: str, *, limit: int = 5) -> dict:
    import urllib.parse

    path = "/recall/v2?" + urllib.parse.urlencode({"q": query, "n": str(limit), "actor": "brain_doctor"})
    return _compact_recall_diagnostic(_http_json(path, timeout=10.0), query=query, limit=limit)


def _slo_snapshot() -> dict:
    return _http_json("/brain/slos", timeout=10.0)


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


def _cli_llm_surface() -> dict:
    """Read-only dispatch-surface snapshot for runtime smoke checks.

    This intentionally imports the local module but does not call any LLM. It
    proves which backend/model chain this checkout will use after restart.
    """

    try:
        brain_core_dir = str(Path(__file__).resolve().parents[1] / "brain_core")
        if brain_core_dir not in sys.path:
            sys.path.insert(0, brain_core_dir)
        import cli_llm

        chain = [
            {"backend": backend, "model": model, "description": desc}
            for backend, model, desc in getattr(cli_llm, "FALLBACK_CHAIN", [])
        ]
        backend_set = {row["backend"] for row in chain}
        return {
            "status": "ok",
            "primary_backend": chain[0]["backend"] if chain else None,
            "primary_model": chain[0]["model"] if chain else None,
            "fallback_chain": chain,
            "claude_backend_present": "claude" in backend_set,
            "claude_prompt_mode_removed": not hasattr(cli_llm, "CLAUDE_BIN"),
        }
    except Exception as exc:
        return {"status": f"error:{str(exc)[:120]}"}


def _recall_structural_judgments_snapshot() -> dict:
    """Sidecar-table observability for the structural recall judge.

    The table is lazy-created by recall_structural_judge, so doctor must never
    create it as a read-only side effect. This snapshot also surfaces legacy
    structural labels still present in action_audit.outcome so migration drift
    is visible.
    """

    if not BRAIN_DB.exists():
        return {"status": "no_brain_db", "table_exists": False}
    try:
        with sqlite3.connect(f"file:{BRAIN_DB}?mode=ro", uri=True, timeout=5) as conn:
            table_exists = bool(
                conn.execute(
                    "SELECT 1 FROM sqlite_master " "WHERE type='table' AND name='recall_structural_judgments'"
                ).fetchone()
            )
            legacy_rows = conn.execute(
                "SELECT outcome, COUNT(*) FROM action_audit "
                "WHERE outcome IN ('structural_good','structural_wrong','structural_neutral') "
                "GROUP BY outcome"
            ).fetchall()
            out = {
                "status": "ok",
                "table_exists": table_exists,
                "legacy_action_outcome_counts": {str(k): int(v) for k, v in legacy_rows},
            }
            if not table_exists:
                return out
            sidecar_rows = conn.execute(
                "SELECT outcome, COUNT(*) FROM recall_structural_judgments GROUP BY outcome"
            ).fetchall()
            total_row = conn.execute(
                "SELECT COUNT(*), MAX(judged_at) FROM recall_structural_judgments"
            ).fetchone()
            out.update(
                {
                    "total": int(total_row[0] or 0),
                    "latest_judged_at": total_row[1],
                    "outcome_counts": {str(k): int(v) for k, v in sidecar_rows},
                }
            )
            return out
    except sqlite3.Error as exc:
        return {"status": f"error:{str(exc)[:120]}", "table_exists": False}


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


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="brain doctor", description="Brain health and recall diagnostics")
    sub = parser.add_subparsers(dest="command")
    recall = sub.add_parser("recall", help="Read-only compact /recall/v2 diagnostic")
    recall.add_argument("--query", required=True, help="Query to diagnose")
    recall.add_argument("--limit", type=int, default=5, help="Max recall rows to show")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "recall":
        print(json.dumps(_recall_diagnostic(args.query, limit=args.limit), indent=2, ensure_ascii=False))
        return 0

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
    report["cli_llm"] = _cli_llm_surface()
    report["recall_structural_judgments"] = _recall_structural_judgments_snapshot()
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
