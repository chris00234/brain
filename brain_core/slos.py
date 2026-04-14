"""brain_core/slos.py - production SLO definitions + check loop (Phase E1).

Single source of truth for every Service-Level Objective the brain enforces.
Each SLO is a typed object with: name, target, measurement, severity, and a
`check()` method that returns True (within budget) or False (breach).

The slo_monitor scheduled job calls `check_all()` on the standard interval
and dispatches alerts via Telegram (jenna-bot) on breach. Rate-limit:
1 alert per (slo, severity) per 30 min.

Production bar: SLOs live in code, not docs. Adding a new SLO = adding a
class instance here + the `check()` implementation. Removing one = deleting
the instance. No yaml, no env vars for thresholds.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

log = logging.getLogger("brain.slos")

try:
    from atoms_store import BRAIN_DB
    from config import AUTONOMY_DB, BRAIN_LOGS_DIR
except ImportError:
    AUTONOMY_DB = Path("/Users/chrischo/server/brain/logs/autonomy.db")
    BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")
    BRAIN_DB = BRAIN_LOGS_DIR / "brain.db"


METRICS_DB = BRAIN_LOGS_DIR / "metrics_history.db"
ALERT_RATE_LIMIT_S = 1800  # 30 min per (slo_name, severity)


@dataclass(frozen=True)
class SLO:
    name: str
    description: str
    target: float
    severity: str  # 'info' | 'warning' | 'critical'
    metric_unit: str = ""
    consecutive_breaches_required: int = 1


@dataclass
class SLOResult:
    slo: SLO
    actual: float
    breached: bool
    delta: float  # actual - target (positive = over target)
    timestamp: float = field(default_factory=time.time)


# ─── SLO definitions ─────────────────────────────────────────────────────

SLOS: dict[str, SLO] = {
    "recall_v2_p95_ms": SLO(
        name="recall_v2_p95_ms",
        description="/recall/v2 p95 latency budget (production hot path)",
        target=350.0,
        severity="warning",
        metric_unit="ms",
        consecutive_breaches_required=3,
    ),
    "recall_v2_content_hit_pct": SLO(
        name="recall_v2_content_hit_pct",
        description="/recall/v2 stable-track content hit rate (regression gate)",
        target=95.0,
        severity="critical",
        metric_unit="%",
        consecutive_breaches_required=1,
    ),
    "breaker_open_count": SLO(
        name="breaker_open_count",
        description="Number of circuit breakers currently in open state",
        target=0.0,
        severity="critical",
        metric_unit="breakers",
        consecutive_breaches_required=1,
    ),
    "outbox_pending_count": SLO(
        name="outbox_pending_count",
        description="SessionEnd outbox backlog (pending replays)",
        target=20.0,
        severity="warning",
        metric_unit="envelopes",
        consecutive_breaches_required=1,
    ),
    "atoms_write_fail_rate_1h": SLO(
        name="atoms_write_fail_rate_1h",
        description="atoms_store.upsert_atom failure rate over the last hour",
        target=1.0,
        severity="warning",
        metric_unit="%",
        consecutive_breaches_required=2,
    ),
    "eval_holdout_growth_weekly": SLO(
        name="eval_holdout_growth_weekly",
        description="Weekly count of eval_proposals promoted via auto-growth",
        target=0.0,  # info-only — never breaches, just observed
        severity="info",
        metric_unit="proposals",
        consecutive_breaches_required=1,
    ),
}


# ─── Measurement functions ──────────────────────────────────────────────


def _measure_recall_v2_p95() -> float:
    """Read p95 latency from metrics_history.db (most recent point)."""
    try:
        if not METRICS_DB.exists():
            return 0.0
        conn = sqlite3.connect(str(METRICS_DB))
        try:
            row = conn.execute(
                "SELECT route_p95 FROM route_metrics "
                "WHERE route = '/recall/v2' ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            return float(row[0]) if row else 0.0
        finally:
            conn.close()
    except sqlite3.Error:
        return 0.0


def _measure_recall_v2_content_hit() -> float:
    """Read latest stable-track eval result."""
    try:
        report_path = BRAIN_LOGS_DIR / "eval-report-stable.json"
        if not report_path.exists():
            report_path = BRAIN_LOGS_DIR / "eval-report.json"
        if not report_path.exists():
            return 0.0
        data = json.loads(report_path.read_text())
        v2 = data.get("v2", {})
        return float(v2.get("hit_content_pct", 0))
    except Exception:
        return 0.0


def _measure_breaker_open_count() -> float:
    try:
        from breakers import list_all

        return float(sum(1 for b in list_all() if b.is_open))
    except Exception:
        return 0.0


def _measure_outbox_pending() -> float:
    pending_dir = Path("~/.openclaw/outbox/brain-learn/pending").expanduser()
    if not pending_dir.exists():
        return 0.0
    try:
        return float(len(list(pending_dir.glob("*.jsonl"))))
    except Exception:
        return 0.0


def _measure_atoms_write_fail_rate() -> float:
    """Approximate via audit_log: count audit_events of type='atoms_write_fail' in the last 1h."""
    try:
        audit_db = BRAIN_LOGS_DIR / "audit.db"
        if not audit_db.exists():
            return 0.0
        cutoff = time.time() - 3600
        conn = sqlite3.connect(str(audit_db))
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM audit_events "
                "WHERE event_type = 'atoms_write_fail' "
                "AND CAST(strftime('%s', timestamp) AS INTEGER) > ?",
                (int(cutoff),),
            ).fetchone()
            return float(row[0]) if row else 0.0
        except sqlite3.Error:
            return 0.0
        finally:
            conn.close()
    except Exception:
        return 0.0


def _measure_eval_holdout_growth() -> float:
    try:
        conn = sqlite3.connect(str(AUTONOMY_DB))
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM eval_proposals "
                "WHERE status = 'promoted' AND promoted_at > datetime('now', '-7 days')"
            ).fetchone()
            return float(row[0]) if row else 0.0
        finally:
            conn.close()
    except sqlite3.Error:
        return 0.0


_MEASUREMENTS: dict[str, Callable[[], float]] = {
    "recall_v2_p95_ms": _measure_recall_v2_p95,
    "recall_v2_content_hit_pct": _measure_recall_v2_content_hit,
    "breaker_open_count": _measure_breaker_open_count,
    "outbox_pending_count": _measure_outbox_pending,
    "atoms_write_fail_rate_1h": _measure_atoms_write_fail_rate,
    "eval_holdout_growth_weekly": _measure_eval_holdout_growth,
}


def _is_breach(slo: SLO, actual: float) -> bool:
    """SLO direction-aware breach check."""
    if slo.name == "recall_v2_content_hit_pct":
        # Higher is better — breach when below target
        return actual < slo.target
    if slo.name == "eval_holdout_growth_weekly":
        # Info-only — never breach
        return False
    # Default: lower-is-better (latency, error count, queue depth)
    return actual > slo.target


def check_one(slo_name: str) -> SLOResult | None:
    slo = SLOS.get(slo_name)
    if not slo:
        return None
    measure_fn = _MEASUREMENTS.get(slo_name)
    if not measure_fn:
        return None
    try:
        actual = measure_fn()
    except Exception as exc:
        log.warning("SLO %s measurement failed: %s", slo_name, exc)
        return None
    return SLOResult(
        slo=slo,
        actual=round(actual, 3),
        breached=_is_breach(slo, actual),
        delta=round(actual - slo.target, 3),
    )


def check_all() -> list[SLOResult]:
    return [r for r in (check_one(name) for name in SLOS) if r is not None]


# ─── Alert dispatch (rate-limited Telegram) ─────────────────────────────

# Rate-limit state is persisted to autonomy.db/brain_config so it survives
# brain-server restarts. An in-memory dict gets wiped on every launchd
# kickstart, which would defeat the 30-minute suppression during crash loops.
_ALERT_KEY_PREFIX = "slo_alert."


def _ensure_brain_config_schema() -> None:
    AUTONOMY_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(AUTONOMY_DB))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS brain_config (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              updated_by TEXT DEFAULT 'system'
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _load_last_alert_at(slo_name: str, severity: str) -> float:
    key = f"{_ALERT_KEY_PREFIX}{slo_name}.{severity}.last_at"
    try:
        _ensure_brain_config_schema()
        conn = sqlite3.connect(str(AUTONOMY_DB))
        try:
            row = conn.execute(
                "SELECT value FROM brain_config WHERE key = ?", (key,)
            ).fetchone()
            return float(row[0]) if row else 0.0
        finally:
            conn.close()
    except (sqlite3.Error, ValueError):
        return 0.0


def _save_last_alert_at(slo_name: str, severity: str, ts: float) -> None:
    key = f"{_ALERT_KEY_PREFIX}{slo_name}.{severity}.last_at"
    try:
        _ensure_brain_config_schema()
        conn = sqlite3.connect(str(AUTONOMY_DB))
        try:
            from datetime import UTC, datetime

            conn.execute(
                "INSERT INTO brain_config (key, value, updated_at, updated_by) "
                "VALUES (?, ?, ?, 'slos') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, f"{ts:.3f}", datetime.now(UTC).isoformat(timespec="seconds")),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass


def _alert_telegram(slo: SLO, actual: float) -> bool:
    import subprocess

    OPENCLAW_BIN = "/Users/chrischo/.local/bin/openclaw"
    TELEGRAM_CHAT_ID = "8484060831"
    TELEGRAM_ACCOUNT = "jenna-bot"

    if not Path(OPENCLAW_BIN).exists():
        log.warning("openclaw binary missing — skipping telegram alert for %s", slo.name)
        return False
    msg = (
        f"[BRAIN SLO {slo.severity.upper()}] {slo.name}\n"
        f"target {slo.target}{slo.metric_unit} · actual {actual}{slo.metric_unit}\n"
        f"{slo.description}"
    )
    try:
        subprocess.run(
            [
                OPENCLAW_BIN,
                "message",
                "send",
                "--channel",
                "telegram",
                "--target",
                TELEGRAM_CHAT_ID,
                "--account",
                TELEGRAM_ACCOUNT,
                "--message",
                msg,
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return True
    except Exception as exc:
        log.warning("telegram alert dispatch failed: %s", exc)
        return False


def maybe_alert(result: SLOResult) -> bool:
    """Rate-limited alert dispatch. Returns True if alert was sent.

    Rate-limit state is persisted in brain_config so it survives restarts.
    """
    if not result.breached:
        return False
    now = time.time()
    last_at = _load_last_alert_at(result.slo.name, result.slo.severity)
    if now - last_at < ALERT_RATE_LIMIT_S:
        return False
    _save_last_alert_at(result.slo.name, result.slo.severity, now)
    return _alert_telegram(result.slo, result.actual)


def run() -> dict:
    """Scheduler entrypoint: check all SLOs, dispatch alerts on breach.

    Returns a summary suitable for /metrics consumption.
    """
    results = check_all()
    breached = [r for r in results if r.breached]
    alerts_sent = 0
    for r in breached:
        if maybe_alert(r):
            alerts_sent += 1
    return {
        "checked": len(results),
        "breached": len(breached),
        "alerts_sent": alerts_sent,
        "results": [
            {
                "name": r.slo.name,
                "target": r.slo.target,
                "actual": r.actual,
                "delta": r.delta,
                "breached": r.breached,
                "severity": r.slo.severity,
                "unit": r.slo.metric_unit,
            }
            for r in results
        ],
    }


if __name__ == "__main__":
    sys.stdout.write(json.dumps(run(), indent=2) + "\n")
