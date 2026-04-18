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
        description="/recall/v2 p95 latency budget (production hot path). Target 500ms reflects realistic warm+parallel-fanout behavior: RAG+canonical+obsidian run parallel ~200-300ms each, plus FTS merge ~100ms, plus rerank. Cold-start queries after brain-server reloads add noise. Warm steady-state is 27-50ms.",
        target=500.0,
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
    # Phase N2 watcher — confidence "pancake" detector. The Bayesian ledger
    # can silently collapse every atom's confidence to the clamp boundaries
    # if the logit-space math is miscalibrated. stddev < 0.05 = every atom
    # is at ~0.02 or ~0.98 with nothing in between.
    "calibration_brier_drift_7d": SLO(
        name="calibration_brier_drift_7d",
        description="W5: week-over-week absolute drift in confidence_calibration reliability_brier (silent miscalibration detector)",
        target=0.05,
        severity="warning",
        metric_unit="brier_delta",
        consecutive_breaches_required=1,
    ),
    "atoms_confidence_stddev_1d": SLO(
        name="atoms_confidence_stddev_1d",
        description="Population stddev of non-obsolete atoms.confidence (pancake detector)",
        target=0.05,  # breach when BELOW
        severity="warning",
        metric_unit="stddev",
        consecutive_breaches_required=2,
    ),
    # Phase N4 watcher — sleep_consolidate wall-clock. Alert if the job
    # starts creeping past 2 minutes — usually means atom_coactivation is
    # approaching the O(n²) cap or the A-MEM step is linking too aggressively.
    "sleep_cycles_duration_1d_p95": SLO(
        name="sleep_cycles_duration_1d_p95",
        description="P95 wall-clock duration of sleep_consolidate over last 24h",
        target=120.0,
        severity="warning",
        metric_unit="seconds",
        consecutive_breaches_required=1,
    ),
    # Phase N3 watcher — runaway auto-graduation. Weekly cap is 5; if this
    # ever hits 6+ we're double-graduating or the lifecycle db is corrupt.
    "holdout_auto_graduation_7d": SLO(
        name="holdout_auto_graduation_7d",
        description="Count of holdout candidates auto-graduated in last 7 days (cap is 5)",
        target=5.0,
        severity="warning",
        metric_unit="candidates",
        consecutive_breaches_required=1,
    ),
    # Phase N4 watcher — coactivation table size. Emergency cap is 100k;
    # warn at 50k so we catch the explosion before the job's own skip kicks in.
    "atom_coactivation_rowcount": SLO(
        name="atom_coactivation_rowcount",
        description="Total rows in atom_coactivation (emergency cap = 100k)",
        target=50_000.0,
        severity="warning",
        metric_unit="rows",
        consecutive_breaches_required=1,
    ),
    # 2026-04-16 watcher — stuck-writer detector. The fail-rate SLO only
    # fires when atoms_store emits audit events; a hung writer (SQLITE_BUSY
    # deadlock, Neo4j timeout) emits ZERO events and fail_rate stays green
    # while no atoms actually land. This floor catches the "suspiciously
    # quiet" case: every active brain usually produces 5+ atoms/hour during
    # waking hours (06:00–23:00 PT). Below that → stuck or ingest down.
    # Higher-is-better SLO — breaches when throughput falls BELOW target.
    "atoms_write_throughput_1h": SLO(
        name="atoms_write_throughput_1h",
        description="atoms_store.upsert_atom successful writes in last 1h (stuck-writer floor)",
        target=5.0,
        severity="warning",
        metric_unit="writes",
        consecutive_breaches_required=2,
    ),
    # 2026-04-17: dispatch failure-rate SLO. The jenna codex session
    # returned empty envelopes 42.5% of the time for 4 days before the
    # breaker finally tripped. Per-agent hourly failure rate catches
    # that degradation 24-48h earlier than breaker_open_count does.
    "dispatch_failure_rate_1h": SLO(
        name="dispatch_failure_rate_1h",
        description="openclaw_dispatch empty-envelope / error rate over the last hour (per all agents)",
        target=20.0,  # breach when >20% of dispatches fail
        severity="warning",
        metric_unit="%",
        consecutive_breaches_required=2,
    ),
    # 2026-04-17: agent session file-size watcher. OpenClaw agent
    # sessions accumulate indefinitely as conversational context grows;
    # past 100MB the codex backend starts returning empty responses.
    # Alerts when any single session .jsonl exceeds the threshold so
    # operator can rotate sessions.json → fresh sessionKey.
    "agent_session_max_mb": SLO(
        name="agent_session_max_mb",
        description="Size of the largest live OpenClaw agent session .jsonl file (MB)",
        target=100.0,
        severity="warning",
        metric_unit="MB",
        consecutive_breaches_required=1,
    ),
}


# ─── Measurement functions ──────────────────────────────────────────────


_RECALL_MIN_SAMPLES = 30  # guard against cold-boot snapshots with a handful of warmup hits


def _measure_recall_v2_p95() -> float:
    """Read p95 latency for /recall/v2 from metrics_snapshots.

    Walks snapshots newest-first and returns the first p95 backed by at least
    `_RECALL_MIN_SAMPLES` samples. Falls back to /recall (v1) within the same
    row before advancing to the next snapshot. Returns 0.0 when no qualifying
    snapshot exists (0 < 350 target = no spurious breach) — keeps the gauge
    silent until real warmup data lands rather than paging on 7 cold hits.
    """
    try:
        if not METRICS_DB.exists():
            return 0.0
        conn = sqlite3.connect(str(METRICS_DB))
        try:
            rows = conn.execute(
                "SELECT payload FROM metrics_snapshots " "ORDER BY id DESC LIMIT 20"
            ).fetchall()
            for (payload_str,) in rows:
                try:
                    payload = json.loads(payload_str)
                except (json.JSONDecodeError, TypeError) as _exc:
                    log.debug("silenced exception in slos.py: %s", _exc)
                    continue
                routes = payload.get("routes", {}) or {}
                v2 = routes.get("/recall/v2") or {}
                if v2.get("count", 0) >= _RECALL_MIN_SAMPLES and v2.get("p95_ms") is not None:
                    return float(v2["p95_ms"])
                v1 = routes.get("/recall") or {}
                if v1.get("count", 0) >= _RECALL_MIN_SAMPLES and v1.get("p95_ms") is not None:
                    return float(v1["p95_ms"])
            return 0.0
        finally:
            conn.close()
    except (sqlite3.Error, json.JSONDecodeError, ValueError, TypeError):
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


def _measure_atoms_write_throughput() -> float:
    """Count successful atoms upserts in the last hour.

    This SLO's INTENT is to catch a stuck atoms writer — not to measure
    overall brain activity. Prior implementation conflated the two:
    "no work to do" and "writer is hung" both produced throughput=0,
    causing daily false-positive breaches during natural morning idle
    windows when raw_events aren't arriving yet.

    Root-cause fix (2026-04-17): differentiate the two cases via input
    signal. The writer is stuck ONLY when:
        raw_events_last_1h > 5  (input arriving)
        AND atoms_last_1h < 5   (no output being produced)

    If raw_events < 5 too, the system is legitimately quiet — return the
    floor value so the SLO stays healthy. This is not a threshold tweak:
    it's fixing a semantic bug in what the SLO measures. The failure
    mode it was designed to catch (writer hung while inputs pile up)
    is still caught, with higher signal-to-noise.

    Still skips 23:00-06:00 PT as those are the nightly batch window
    where the writer is legitimately busy with scheduled pipelines.
    """
    try:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo

        hour_pt = _dt.now(ZoneInfo("America/Los_Angeles")).hour
        if hour_pt < 6 or hour_pt >= 23:
            return 5.0
    except Exception as _exc:
        log.debug("silenced exception in slos.py: %s", _exc)
    try:
        if not BRAIN_DB.exists():
            return 0.0
        cutoff = time.time() - 3600
        conn = sqlite3.connect(str(BRAIN_DB))
        try:
            atoms_row = conn.execute(
                "SELECT COUNT(*) FROM atoms " "WHERE CAST(strftime('%s', updated_at) AS INTEGER) > ?",
                (int(cutoff),),
            ).fetchone()
            atoms_written = float(atoms_row[0]) if atoms_row else 0.0

            # Check input queue: raw_events created in the same window.
            # If no input, the system is quiet — not a stuck writer.
            raw_row = conn.execute(
                "SELECT COUNT(*) FROM raw_events " "WHERE CAST(strftime('%s', created_at) AS INTEGER) > ?",
                (int(cutoff),),
            ).fetchone()
            raw_in = float(raw_row[0]) if raw_row else 0.0

            if raw_in < 5.0:
                # No meaningful input → can't be a stuck writer. Report
                # the floor so the SLO stays healthy until inputs resume.
                return 5.0
            return atoms_written
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


def _measure_atoms_confidence_stddev() -> float:
    """Phase N2 pancake detector. Returns the population stddev of
    atoms.confidence across non-obsolete atoms. If the Bayesian ledger
    collapses everything to [0.02, 0.98], stddev drops below ~0.05 and
    the SLO fires.
    """
    try:
        conn = sqlite3.connect(str(BRAIN_DB))
        try:
            row = conn.execute(
                "SELECT COUNT(*), AVG(confidence), "
                "SUM(confidence * confidence) "
                "FROM atoms WHERE tier != 'obsolete'"
            ).fetchone()
            if not row or not row[0]:
                return 0.5
            n, mean, sum_sq = row
            mean = float(mean or 0.0)
            variance = max(0.0, (float(sum_sq or 0.0) / n) - (mean * mean))
            return round(variance**0.5, 4)
        finally:
            conn.close()
    except sqlite3.Error:
        return 0.5  # assume healthy on read error — don't page


def _measure_sleep_cycles_duration_p95() -> float:
    """Phase N4 watcher. Reads sleep_cycles rows from the last 24h and
    computes the p95 of (ended_at - started_at). Returns 0 when no rows
    exist (no cycles yet = no alert).
    """
    try:
        conn = sqlite3.connect(str(BRAIN_DB))
        try:
            rows = conn.execute(
                "SELECT (julianday(ended_at) - julianday(started_at)) * 86400 AS secs "
                "FROM sleep_cycles "
                "WHERE ended_at IS NOT NULL "
                "AND started_at >= datetime('now', '-1 day') "
                "ORDER BY secs ASC"
            ).fetchall()
            if not rows:
                return 0.0
            seconds = [float(r[0] or 0.0) for r in rows]
            idx = max(0, int(len(seconds) * 0.95) - 1)
            return round(seconds[idx], 2)
        finally:
            conn.close()
    except sqlite3.Error:
        return 0.0


def _measure_holdout_auto_graduation_7d() -> float:
    """Phase N3 watcher. Count rows in eval_holdout_lifecycle auto_stable_at
    >= now - 7 days. Sanity check against the weekly cap of 5.
    """
    try:
        conn = sqlite3.connect(str(BRAIN_DB))
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM eval_holdout_lifecycle "
                "WHERE auto_stable_at IS NOT NULL "
                "AND auto_stable_at >= datetime('now', '-7 days')"
            ).fetchone()
            return float(row[0]) if row else 0.0
        finally:
            conn.close()
    except sqlite3.Error:
        return 0.0


def _measure_atom_coactivation_rowcount() -> float:
    """Phase N4 watcher. Alert when atom_coactivation approaches the 100k
    emergency cap so we can DELETE WHERE n_events < 2 before the nightly
    job starts skipping upserts.
    """
    try:
        conn = sqlite3.connect(str(BRAIN_DB))
        try:
            row = conn.execute("SELECT COUNT(*) FROM atom_coactivation").fetchone()
            return float(row[0]) if row else 0.0
        finally:
            conn.close()
    except sqlite3.Error:
        return 0.0


def _measure_dispatch_failure_rate_1h() -> float:
    """Read dispatch-failures.jsonl for last hour vs total dispatches.

    Counts entries in logs/dispatch-failures.jsonl with timestamp
    within the last 60 minutes and divides by total dispatches over
    the same window (pulled from llm_usage.db). Returns 0 if no
    dispatches occurred (can't divide)."""
    try:
        import datetime as _dt

        failures_path = BRAIN_LOGS_DIR / "dispatch-failures.jsonl"
        if not failures_path.exists():
            return 0.0
        cutoff = _dt.datetime.now(_dt.UTC) - _dt.timedelta(hours=1)
        cutoff_iso = cutoff.isoformat()

        fail_count = 0
        # Tail-read: only scan last ~100KB for recent entries
        with failures_path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 100_000))
            tail = f.read().decode("utf-8", errors="ignore")
        for line in tail.splitlines():
            if not line.strip().startswith("{"):
                continue
            try:
                rec = json.loads(line)
            except Exception as _exc:
                log.debug("silenced exception in slos.py: %s", _exc)
                continue
            ts = rec.get("timestamp", "")
            if ts and ts >= cutoff_iso:
                fail_count += 1

        # Total dispatches from llm_usage.db
        try:
            llm_db = BRAIN_LOGS_DIR / "llm_usage.db"
            if not llm_db.exists():
                return 0.0
            with sqlite3.connect(str(llm_db)) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM llm_usage WHERE ts_utc >= ?",
                    (cutoff_iso,),
                ).fetchone()
            total = int(row[0] or 0) if row else 0
        except Exception:
            total = 0

        if total == 0:
            return 0.0
        return round(100.0 * fail_count / max(total, 1), 2)
    except Exception as exc:
        log.debug("dispatch_failure_rate measurement failed: %s", exc)
        return 0.0


def _measure_agent_session_max_mb() -> float:
    """Largest live OpenClaw agent session .jsonl file size in MB."""
    try:
        agents_root = Path.home() / ".openclaw" / "agents"
        if not agents_root.exists():
            return 0.0
        max_bytes = 0
        for agent_dir in agents_root.iterdir():
            sessions = agent_dir / "sessions"
            if not sessions.is_dir():
                continue
            for jsonl in sessions.glob("*.jsonl"):
                if ".checkpoint." in jsonl.name:
                    continue
                try:
                    max_bytes = max(max_bytes, jsonl.stat().st_size)
                except OSError as _exc:
                    log.debug("silenced exception in slos.py: %s", _exc)
                    continue
        return round(max_bytes / (1024 * 1024), 1)
    except Exception as exc:
        log.debug("agent_session_max_mb measurement failed: %s", exc)
        return 0.0


_MEASUREMENTS: dict[str, Callable[[], float]] = {
    "recall_v2_p95_ms": _measure_recall_v2_p95,
    "recall_v2_content_hit_pct": _measure_recall_v2_content_hit,
    "breaker_open_count": _measure_breaker_open_count,
    "outbox_pending_count": _measure_outbox_pending,
    "atoms_write_fail_rate_1h": _measure_atoms_write_fail_rate,
    "atoms_write_throughput_1h": _measure_atoms_write_throughput,
    "eval_holdout_growth_weekly": _measure_eval_holdout_growth,
    "atoms_confidence_stddev_1d": _measure_atoms_confidence_stddev,
    "sleep_cycles_duration_1d_p95": _measure_sleep_cycles_duration_p95,
    "holdout_auto_graduation_7d": _measure_holdout_auto_graduation_7d,
    "atom_coactivation_rowcount": _measure_atom_coactivation_rowcount,
    "calibration_brier_drift_7d": lambda: _measure_calibration_drift(),
    "dispatch_failure_rate_1h": _measure_dispatch_failure_rate_1h,
    "agent_session_max_mb": _measure_agent_session_max_mb,
}


def _measure_calibration_drift() -> float:
    """W5 (2026-04-17): read the last confidence_calibration.run() drift_brier.

    Confidence calibration refits weekly from eval_holdout + outcomes data.
    A sudden week-over-week brier shift means the underlying atom-confidence
    distribution moved — either a real event (new data source / LoRA swap)
    or silent miscalibration (the self-learning loop going out of tune).
    Either deserves a human glance.
    """
    try:
        import brain_config_store

        raw = brain_config_store.get("confidence_calibration.drift_brier")
        if not raw:
            return 0.0
        import json as _json

        data = _json.loads(raw)
        return float(data.get("drift", 0.0))
    except Exception:
        return 0.0


def _is_breach(slo: SLO, actual: float) -> bool:
    """SLO direction-aware breach check."""
    if slo.name == "recall_v2_content_hit_pct":
        # Higher is better — breach when below target
        return actual < slo.target
    if slo.name == "atoms_confidence_stddev_1d":
        # Phase N2 pancake — higher is better; breach when below target
        return actual < slo.target
    if slo.name == "atoms_write_throughput_1h":
        # 2026-04-16 — higher-is-better throughput floor; breach on quiet writer
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

# Rate-limit state is persisted to autonomy.db/brain_config via the shared
# brain_config_store so it survives brain-server restarts. An in-memory dict
# would be wiped on every launchd kickstart, defeating the 30-minute
# suppression during crash loops.
_ALERT_KEY_PREFIX = "slo_alert."


def _load_last_alert_at(slo_name: str, severity: str) -> float:
    key = f"{_ALERT_KEY_PREFIX}{slo_name}.{severity}.last_at"
    try:
        import brain_config_store

        value = brain_config_store.get(key)
        return float(value) if value else 0.0
    except (sqlite3.Error, ValueError):
        return 0.0


def _save_last_alert_at(slo_name: str, severity: str, ts: float) -> None:
    key = f"{_ALERT_KEY_PREFIX}{slo_name}.{severity}.last_at"
    try:
        import brain_config_store

        brain_config_store.set(key, f"{ts:.3f}", updated_by="slos")
    except sqlite3.Error as _exc:
        log.debug("silenced exception in slos.py: %s", _exc)


def _alert_telegram(slo: SLO, actual: float) -> bool:
    """Delegate to the unified telegram_alert module (2026-04-17)."""
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).parent))
    from telegram_alert import send_chris_telegram

    msg = (
        f"[BRAIN SLO {slo.severity.upper()}] {slo.name}\n"
        f"target {slo.target}{slo.metric_unit} · actual {actual}{slo.metric_unit}\n"
        f"{slo.description}"
    )
    severity = "critical" if slo.severity == "critical" else "warn"
    return send_chris_telegram(msg, source=f"slo:{slo.name}", severity=severity)


_QUIET_HOUR_EXCEPTIONS = {"breaker_open_count", "atoms_write_fail_rate_1h"}


def _in_quiet_hours_now() -> bool:
    """Gate Telegram alerts during configured quiet hours.

    Reads the persisted quiet_hours.* keys from brain_config directly —
    autonomy.QUIET_HOURS is a module-level constant that never picks up
    POST /brain/quiet-hours overrides, so Chris's configured 22:30-07:30
    window was being silently ignored by subprocesses.

    Only truly urgent SLOs (data-loss or infra-down) bypass quiet hours;
    everything else holds until morning so Chris isn't woken up by
    slow-moving breaches like recall latency creep.
    """
    try:
        from datetime import datetime as _dt
        from datetime import time as _dtime
        from zoneinfo import ZoneInfo

        import brain_config_store

        # 2026-04-17 fix: defaults MUST match the documented operational window
        # (22:30–07:30 PT). The prior 23:00/07:00 fallbacks silently narrowed
        # quiet hours by 30 min on each end when brain_config_store was
        # unreachable, waking Chris up at 22:31 / 07:01.
        start_s = brain_config_store.get("quiet_hours.start") or "22:30"
        end_s = brain_config_store.get("quiet_hours.end") or "07:30"
        tz_s = brain_config_store.get("quiet_hours.tz") or "America/Los_Angeles"
        start = _dtime.fromisoformat(start_s)
        end = _dtime.fromisoformat(end_s)
        t = _dt.now(ZoneInfo(tz_s)).time()
        if start > end:  # wraps midnight
            return t >= start or t < end
        return start <= t < end
    except Exception:
        return False


def maybe_alert(result: SLOResult) -> bool:
    """Rate-limited alert dispatch. Returns True if alert was sent.

    Rate-limit state is persisted in brain_config so it survives restarts.
    """
    if not result.breached:
        return False
    if result.slo.name not in _QUIET_HOUR_EXCEPTIONS and _in_quiet_hours_now():
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
