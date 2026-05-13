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
import os
import shlex
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

log = logging.getLogger("brain.slos")

try:
    from atoms_store import BRAIN_DB

    from config import AUTONOMY_DB, BRAIN_DIR, BRAIN_LOGS_DIR
except ImportError:
    BRAIN_DIR = Path("/Users/chrischo/server/brain")
    AUTONOMY_DB = Path("/Users/chrischo/server/brain/logs/autonomy.db")
    BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")
    BRAIN_DB = BRAIN_LOGS_DIR / "brain.db"


METRICS_DB = BRAIN_LOGS_DIR / "metrics_history.db"
ALERT_RATE_LIMIT_S = 1800  # 30 min per (slo_name, severity)
DAILY_JOB_ALERT_RATE_LIMIT_S = 26 * 3600


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
        description="/recall/v2 p95 latency budget (production hot path). Loosened 2026-04-24 from 500ms to 1000ms because 350-500ms produced noisy warnings while the quality-critical path legitimately includes search fan-out plus Korean multilingual reranking. 1000ms is the operator-facing ceiling: alert only when recall becomes meaningfully slow, while accuracy SLOs remain the primary regression gate. Tightening back requires either a fast-mode route, lighter reranker, or measured latency headroom across normal agent load.",
        target=1000.0,
        severity="warning",
        metric_unit="ms",
        consecutive_breaches_required=3,
    ),
    "recall_v2_content_hit_pct": SLO(
        name="recall_v2_content_hit_pct",
        description="/recall/v2 stable-track content hit rate (regression gate). Raised 2026-04-21 from 95% to 96% — hybrid rescore + int8 rescoring measured 98.6% stable, so a drop below 96 indicates a real retrieval regression not noise.",
        target=96.0,
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
        description=(
            "Population stddev of non-obsolete atoms.confidence (pancake detector). "
            "Target raised 2026-05-13 from 0.05 to 0.10: live distribution is bimodal "
            "at 0.8-0.9 + dream_replay conjecture floor at 0.3, giving healthy "
            "steady-state stddev ≈ 0.14. The 0.05 target was set before the bimodal "
            "shape was characterized and flagged the natural distribution as a "
            "breach. 0.10 sits below the observed floor: anything below it means "
            "the conjecture floor disappeared or all atoms collapsed into one band."
        ),
        target=0.10,  # breach when BELOW; signals real pancake (everything in one band)
        severity="warning",
        metric_unit="stddev",
        consecutive_breaches_required=2,
    ),
    # Phase N4 watcher — sleep_consolidate wall-clock. This daily job needs a
    # latest-cycle signal: after a verified remediation run, stale pre-fix
    # cycles should not keep the system red for the rest of the day.
    "sleep_cycles_duration_1d_p95": SLO(
        name="sleep_cycles_duration_1d_p95",
        description="Latest completed sleep_consolidate wall-clock duration in the last 24h",
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
    # waking hours (06:00-23:00 PT). Below that — stuck or ingest down.
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
    # 2026-04-17: disk watcher. logs/ grew silently (637MB embed cache
    # sat undetected for days). Alert when total size exceeds threshold
    # so we catch silent growth before disk fills.
    "boot_context_degraded_1h": SLO(
        name="boot_context_degraded_1h",
        description="Count of degraded boot-context serves in the last hour. Non-zero = a session started without fresh brain state (cache fallback or brain unreachable). Target raised 2→10 2026-04-23 after subagent flood made 2 unrealistic — each subagent dispatch forks a fresh UserPromptSubmit on its own session_id, and a flurry of 10-15 subagents in an active coding hour is normal. Real degradation looks like sustained 20+ serves/hour.",
        target=10.0,
        severity="warning",
        metric_unit="serves",
        consecutive_breaches_required=2,
    ),
    "self_eval_drift_7d": SLO(
        name="self_eval_drift_7d",
        description="Percentage of sampled recent /recall queries whose top-3 results have Jaccard overlap < 0.7 when re-run. High drift = retrieval quality is shifting in ways SLOs didn't catch. Measured nightly by self_eval drive over last 7d of action_audit samples.",
        target=25.0,  # breach when >25% of samples drift
        severity="warning",
        metric_unit="%",
        consecutive_breaches_required=2,
    ),
    # 2026-05-11: target raised 2048→3072 MB. The original 2GB ceiling was set
    # when brain.db was ~150 MB; today brain.db alone is 401 MB and autonomy.db
    # 290 MB — the SQLite truth layer plus the 96 MiB-capped WAL files
    # (db_maintenance.WAL_JOURNAL_SIZE_LIMIT_BYTES) plus 4 days of local
    # backups (brain_db + docker-volumes, MinIO holds the long-DR window)
    # constitute ~2.0 GB of legitimate steady state. 3072 MB gives natural
    # growth headroom; the SLO still catches WAL leaks and silent log
    # explosions because those move multi-hundred MB at a time. Tightening
    # again only makes sense after a brain.db VACUUM materially shrinks the
    # truth layer or after archival pruning of long-lived raw_events.
    "logs_dir_total_mb": SLO(
        name="logs_dir_total_mb",
        description="Total size of ~/server/brain/logs/ in MB (DBs + journals + job logs + local backups). Target raised 2048→3072 MB 2026-05-11 to match steady-state with brain.db crossing 400 MB and bounded WAL/backup retention.",
        target=3072.0,
        severity="warning",
        metric_unit="MB",
        consecutive_breaches_required=1,
    ),
    # 2026-05-11 growth-rate companion to logs_dir_total_mb. Catches anomalous
    # daily growth (WAL leak, runaway log file, accumulator regression) BEFORE
    # the absolute size budget breaches. Normal day-over-day delta is ~25 MB
    # (50 MB new brain backup + 20 MB new autonomy backup - ~45 MB pruned
    # by retention - some VACUUM reclaim). 100 MB/day target gives 4x normal
    # headroom while flagging real anomalies. Snapshots come from the daily
    # WAL checkpoint job, so this signal lags real growth by at most 24h.
    "logs_dir_growth_24h_mb": SLO(
        name="logs_dir_growth_24h_mb",
        description="24-hour delta in ~/server/brain/logs/ size. Catches WAL leaks, accumulator regressions, and runaway log writes BEFORE the absolute logs_dir_total_mb budget breaches.",
        target=100.0,
        severity="warning",
        metric_unit="MB/24h",
        consecutive_breaches_required=1,
    ),
    "entry_contract_missing_pct": SLO(
        name="entry_contract_missing_pct",
        description="Percentage of sampled live Qdrant points missing the v2 entry contract (schema/chunk/tag/provenance fields). Any non-zero value means a write path bypassed the source-aware boundary or a backfill regressed.",
        target=0.0,
        severity="critical",
        metric_unit="%",
        consecutive_breaches_required=1,
    ),
    "telegram_backlog_pending_count": SLO(
        name="telegram_backlog_pending_count",
        description="Pending direct Telegram alert backlog rows. Any pending row means Chris-facing alert delivery failed and needs replay before it goes stale.",
        target=0.0,
        severity="critical",
        metric_unit="alerts",
        consecutive_breaches_required=1,
    ),
    "telegram_direct_health": SLO(
        name="telegram_direct_health",
        description="Direct Telegram Bot API healthcheck for Jenna token + Chris chat reachability. 0=healthy, 1=unhealthy. This verifies the alert path without sending a daily test DM.",
        target=0.0,
        severity="critical",
        metric_unit="failed",
        consecutive_breaches_required=1,
    ),
    "openclaw_gateway_health": SLO(
        name="openclaw_gateway_health",
        description="OpenClaw local gateway TCP health on 127.0.0.1:18789. 0=connectable, 1=unreachable. Agent handoff tasks depend on this gateway; a breach means automated OpenClaw agent work may be only queued/deferred rather than actually running.",
        target=0.0,
        severity="critical",
        metric_unit="failed",
        consecutive_breaches_required=1,
    ),
    "task_dispatch_stale_started_count": SLO(
        name="task_dispatch_stale_started_count",
        description="Count of task_dispatch_attempts stuck in started for more than 15 minutes. Non-zero means Brain may have crashed mid-agent dispatch and task execution truth needs recovery.",
        target=0.0,
        severity="warning",
        metric_unit="attempts",
        consecutive_breaches_required=1,
    ),
    "task_failure_lesson_missing_count": SLO(
        name="task_failure_lesson_missing_count",
        description="Failed/deferred task dispatch attempts older than 15 minutes without a recorded Reflexion failure lesson. Non-zero means Brain is repeating autonomous failures without durable lessons.",
        target=0.0,
        severity="warning",
        metric_unit="attempts",
        consecutive_breaches_required=1,
    ),
    "autonomous_work_visibility_gap_count": SLO(
        name="autonomous_work_visibility_gap_count",
        description=(
            "Recent concrete autonomous/background work records missing UI/postmortem evidence fields. "
            "Non-zero means Brain did background work without enough visible traceability."
        ),
        target=0.0,
        severity="critical",
        metric_unit="records",
        consecutive_breaches_required=1,
    ),
    # 2026-04-21: qdrant-backup silent-failure watcher. The nightly
    # ai.openclaw.qdrant-backup launchd plist runs at 03:00 local time and
    # uploads to MinIO. If it silently fails (S3 creds rot, Qdrant snapshot
    # API flakes, or the Python CLI itself crashes pre-upload), there is
    # no natural alarm — the next cron fire just produces a fresh attempt
    # and the gap goes unobserved. This floor breaches when the most
    # recent local qdrant-backup-*.tar.gz is older than 36h, catching a
    # full failed cycle before a second day compounds the risk.
    "qdrant_backup_age_hours": SLO(
        name="qdrant_backup_age_hours",
        description="Age in hours of the most recent qdrant-backup-*.tar.gz. Breaches >36h (one full missed nightly backup window).",
        target=36.0,
        severity="warning",
        metric_unit="hours",
        consecutive_breaches_required=1,
    ),
    # Parity with qdrant_backup_age — Neo4j entity graph is as durability-
    # critical as the vector store, and backup_neo4j has no post-upload
    # verification of its own. Age watcher catches a silently failing chain.
    "neo4j_backup_age_hours": SLO(
        name="neo4j_backup_age_hours",
        description="Age in hours of the most recent neo4j-backup-*.tar.gz in MinIO. Breaches >36h.",
        target=36.0,
        severity="warning",
        metric_unit="hours",
        consecutive_breaches_required=1,
    ),
    "backup_restore_drill_age_hours": SLO(
        name="backup_restore_drill_age_hours",
        description="Age in hours of the latest successful SQLite backup restore-readiness drill. Breaches >192h so weekly drill failures surface before backups become untrusted.",
        target=192.0,
        severity="warning",
        metric_unit="hours",
        consecutive_breaches_required=1,
    ),
    # 2026-04-26: brain server RSS leak detector. Pre-fix the long-running
    # server.py grew to ~4 GB after 17h because torch.mps.empty_cache() was
    # gated behind BRAIN_CE_MPS_EMPTY_CACHE=true (default false), so PyTorch's
    # MPS allocator held GPU scratch buffers indefinitely. Target 3072 MB
    # (~3 GB physical footprint) gives steady-state headroom — post-fix the
    # process should sit at ~1.3 GB. Breaches signal the leak returned or a
    # new accumulator landed.
    "brain_server_rss_mb": SLO(
        name="brain_server_rss_mb",
        description="Brain FastAPI server RSS in MB (ps-reported, includes mmap overhead). Target 3072 = ~3 GB physical footprint. Breaches signal the MPS empty_cache leak returned or a new in-process accumulator was introduced.",
        target=3072.0,
        severity="warning",
        metric_unit="MB",
        consecutive_breaches_required=2,
    ),
}


# ─── Measurement functions ──────────────────────────────────────────────


_RECALL_MIN_SAMPLES = 30  # guard against cold-boot snapshots with a handful of warmup hits
_RECALL_AGENT_ACTORS = {
    "codex",
    "mcp",
    "claude",
    "claude-code",
    "gemini",
    "openclaw",
    "jenna",
    "liz",
    "ellie",
    "sage",
    "market",
    "recall_judge",
    "slo_monitor",
}


def _parse_snapshot_ts(timestamp: str | None) -> datetime | None:
    if not timestamp:
        return None
    raw = str(timestamp).strip()
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except ValueError:
        return None


def _snapshot_recall_v2_internal_only(timestamp: str | None, sample_count: int) -> bool:
    """Return True when a persisted route snapshot is only internal agents.

    The metrics buffer started with just route-level samples, so historical
    snapshots cannot directly distinguish `actor=codex` command bursts from
    human/prod recall. Cross-check action_audit for the same 30-minute metrics
    window: if every audited `/recall/v2` request is an internal actor, the
    production latency SLO should ignore that snapshot instead of paging Chris.
    """

    if sample_count < _RECALL_MIN_SAMPLES:
        return False
    ts = _parse_snapshot_ts(timestamp)
    if ts is None or not BRAIN_DB.exists():
        return False
    start = ts - timedelta(seconds=1800)
    start_s = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_s = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with sqlite3.connect(str(BRAIN_DB)) as conn:
            rows = conn.execute(
                """
                SELECT lower(coalesce(actor, '')) AS actor, count(*) AS n
                FROM action_audit
                WHERE route = '/recall/v2'
                  AND created_at >= ?
                  AND created_at <= ?
                GROUP BY lower(coalesce(actor, ''))
                """,
                (start_s, end_s),
            ).fetchall()
    except sqlite3.Error:
        return False
    total = sum(int(row[1] or 0) for row in rows)
    if total < _RECALL_MIN_SAMPLES:
        return False
    internal = sum(int(row[1] or 0) for row in rows if str(row[0] or "") in _RECALL_AGENT_ACTORS)
    return internal == total


def _live_recall_v2_p95() -> tuple[bool, float]:
    """Return live in-process /recall/v2 p95 when this runs inside FastAPI.

    The scheduled SLO runner can execute out-of-process, in which case the
    metrics buffer has no route samples and we fall back to persisted
    snapshots. When the `/brain/slos` route calls this in the server process,
    however, live route samples are newer and more truthful than old
    metrics_history rows. If live prod traffic exists but has not reached the
    sample floor, return a warmup value instead of reviving a stale persisted
    breach.
    """
    try:
        from metrics_buffer import metrics_buffer

        routes = metrics_buffer.snapshot().get("routes", {}) or {}
        v2 = routes.get("/recall/v2") or {}
        samples = int(v2.get("window_count", v2.get("count", 0)) or 0)
        if samples <= 0:
            return False, 0.0
        if samples < _RECALL_MIN_SAMPLES:
            return True, 0.0
        p95 = v2.get("p95_ms")
        if p95 is None:
            return True, 0.0
        return True, float(p95)
    except Exception:
        return False, 0.0


def _measure_recall_v2_p95() -> float:
    """Read p95 latency for production /recall/v2 traffic.

    Walks snapshots newest-first and returns the first p95 backed by at least
    `_RECALL_MIN_SAMPLES` samples. Falls back to /recall (v1) within the same
    row before advancing to the next snapshot. Returns 0.0 when no qualifying
    snapshot exists (0 < 350 target = no spurious breach) — keeps the gauge
    silent until real warmup data lands rather than paging on 7 cold hits.
    """
    live_available, live_p95 = _live_recall_v2_p95()
    if live_available:
        return live_p95
    try:
        if not METRICS_DB.exists():
            return 0.0
        conn = sqlite3.connect(str(METRICS_DB))
        try:
            rows = conn.execute(
                "SELECT timestamp, payload FROM metrics_snapshots " "ORDER BY id DESC LIMIT 20"
            ).fetchall()
            for timestamp, payload_str in rows:
                try:
                    payload = json.loads(payload_str)
                except (json.JSONDecodeError, TypeError) as _exc:
                    log.debug("silenced exception in slos.py: %s", _exc)
                    continue
                routes = payload.get("routes", {}) or {}
                v2 = routes.get("/recall/v2") or {}
                v2_samples = v2.get("window_count", v2.get("count", 0))
                try:
                    v2_samples_i = int(v2_samples or 0)
                except (TypeError, ValueError):
                    v2_samples_i = 0
                if _snapshot_recall_v2_internal_only(timestamp, v2_samples_i):
                    continue
                if v2_samples_i >= _RECALL_MIN_SAMPLES and v2.get("p95_ms") is not None:
                    return float(v2["p95_ms"])
                v1 = routes.get("/recall") or {}
                v1_samples = v1.get("window_count", v1.get("count", 0))
                if v1_samples >= _RECALL_MIN_SAMPLES and v1.get("p95_ms") is not None:
                    return float(v1["p95_ms"])
            return 0.0
        finally:
            conn.close()
    except (sqlite3.Error, json.JSONDecodeError, ValueError, TypeError):
        return 0.0


def _measure_recall_v2_content_hit() -> float:
    """Read latest stable-track eval result.

    Cold-start safety (2026-05-11): this SLO is *higher-is-better*, so any
    "no data" path that returned 0.0 was a critical-severity false-positive
    waiting to fire. Returning the target (or above) when the eval report
    is missing or unparseable lets the system stay quiet on a fresh install
    and on transient read failures, while a real 0% hit rate from a present
    report still breaches. The eval job's own health is tracked elsewhere
    (eval_holdout_growth_weekly, scheduler history) — this SLO is for the
    retrieval gate, not the eval pipeline.
    """
    try:
        report_path = BRAIN_LOGS_DIR / "eval-report-stable.json"
        if not report_path.exists():
            report_path = BRAIN_LOGS_DIR / "eval-report.json"
        if not report_path.exists():
            return float(SLOS["recall_v2_content_hit_pct"].target)
        data = json.loads(report_path.read_text())
        v2 = data.get("v2") or {}
        if "hit_content_pct" not in v2:
            return float(SLOS["recall_v2_content_hit_pct"].target)
        return float(v2["hit_content_pct"])
    except Exception as exc:
        log.debug("recall_v2_content_hit_pct measurement failed: %s", exc)
        return float(SLOS["recall_v2_content_hit_pct"].target)


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
            # Cold start (fresh install) — no DB yet means no input, no output,
            # and no "stuck writer" can exist. Return the floor like the night
            # branch above so the SLO stays quiet until real data starts flowing.
            return 5.0
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
            #
            # Exclude source_types that don't feed the atoms writer:
            #   - atoms_hot_path  → produced INSIDE upsert_atom (it IS output, not input).
            #     Counting it as input made the SLO compare itself to itself.
            #   - coding_event    → feeds coding_event_outcomes sidecar (revert tracking),
            #     never goes through ingest_classifier or produces atoms.
            # Including these caused false-positive "stuck writer" breaches whenever
            # only VS Code activity or atoms_hot_path provenance rows were flowing.
            raw_row = conn.execute(
                "SELECT COUNT(*) FROM raw_events "
                "WHERE CAST(strftime('%s', created_at) AS INTEGER) > ? "
                "  AND source_type NOT IN ('atoms_hot_path', 'coding_event')",
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
    """Phase N4 watcher. Return the latest completed sleep cycle duration.

    Sleep consolidation is normally once daily. A rolling 24h percentile kept
    alerting on already-remediated slow cycles after a successful verification
    run. Use the latest completed cycle instead, and compare timestamps via
    `julianday()` so ISO `T...Z` rows are not lexicographically misclassified
    against SQLite's space-separated `datetime('now')` string.
    """
    try:
        conn = sqlite3.connect(str(BRAIN_DB))
        try:
            row = conn.execute(
                "SELECT (julianday(ended_at) - julianday(started_at)) * 86400 AS secs "
                "FROM sleep_cycles "
                "WHERE ended_at IS NOT NULL "
                "AND julianday(started_at) >= julianday('now', '-1 day') "
                "ORDER BY julianday(started_at) DESC "
                "LIMIT 1"
            ).fetchone()
            if not row:
                return 0.0
            return round(float(row[0] or 0.0), 2)
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
        # llm_usage.db stores timezone-naive strings like
        # '2026-04-17T21:09:53.050267'. failures.jsonl uses ISO with
        # +00:00 suffix. Normalize both to zone-stripped strings so
        # lexical string comparison works.
        cutoff_dt_naive = _dt.datetime.now(_dt.UTC).replace(tzinfo=None) - _dt.timedelta(hours=1)
        cutoff_str = cutoff_dt_naive.strftime("%Y-%m-%dT%H:%M:%S")

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
            # Strip timezone for apples-to-apples compare
            ts_naive = ts.split("+")[0].split("Z")[0] if ts else ""
            if ts_naive and ts_naive >= cutoff_str:
                fail_count += 1

        # Total dispatches from llm_usage.db (column is `timestamp`)
        try:
            llm_db = BRAIN_LOGS_DIR / "llm_usage.db"
            if not llm_db.exists():
                return 0.0
            with sqlite3.connect(str(llm_db)) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM llm_usage WHERE timestamp >= ?",
                    (cutoff_str,),
                ).fetchone()
            total = int(row[0] or 0) if row else 0
        except Exception as exc:
            log.debug("dispatch_failure_rate llm_usage query failed: %s", exc)
            total = 0

        if total == 0:
            return 0.0
        return round(100.0 * fail_count / max(total, 1), 2)
    except Exception as exc:
        log.debug("dispatch_failure_rate measurement failed: %s", exc)
        return 0.0


def _measure_qdrant_backup_age_hours() -> float:
    """Age of the newest ~/server/brain/qdrant-backups/qdrant-backup-*.tar.gz.

    Returns hours since last successful backup tarball landed on disk. The
    nightly plist runs at 03:00 — a 36h threshold catches a single missed
    cycle without pagering on 3:01am before today's backup completes.
    Returns 999.0 when no backups exist (guaranteed breach) so a fresh
    install surfaces the gap immediately instead of silently succeeding.
    """
    try:
        backup_dir = Path("/Users/chrischo/server/brain/qdrant-backups")
        if not backup_dir.exists():
            return 999.0
        tarballs = list(backup_dir.glob("qdrant-backup-*.tar.gz"))
        if not tarballs:
            return 999.0
        newest = max(tarballs, key=lambda p: p.stat().st_mtime)
        age_s = time.time() - newest.stat().st_mtime
        return round(age_s / 3600.0, 2)
    except Exception as exc:
        log.debug("qdrant_backup_age measurement failed: %s", exc)
        return 999.0


def _measure_neo4j_backup_age_hours() -> float:
    """Age of the newest neo4j-backup-*.tar.gz in MinIO.

    Unlike qdrant backups which stage locally before MinIO upload, the Neo4j
    job uploads straight from a tempdir so we have to query the bucket. A
    36h threshold mirrors the qdrant watcher. 999.0 on any failure so a
    configuration/auth regression surfaces immediately instead of looking
    healthy. MinIO creds come from _minio.s3_client.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cli"))
        from _minio import s3_client as _s3_client

        s3 = _s3_client()
        newest_ts = 0.0
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket="rag-backups", Prefix="neo4j-backup-"):
            for obj in page.get("Contents", []):
                if not obj["Key"].endswith(".tar.gz"):
                    continue
                mtime = obj.get("LastModified")
                if mtime is None:
                    continue
                ts = mtime.timestamp()
                if ts > newest_ts:
                    newest_ts = ts
        if newest_ts == 0.0:
            return 999.0
        age_s = time.time() - newest_ts
        return round(age_s / 3600.0, 2)
    except Exception as exc:
        log.debug("neo4j_backup_age measurement failed: %s", exc)
        return 999.0


def _measure_backup_restore_drill_age_hours() -> float:
    """Age of the latest successful SQLite restore-readiness drill."""
    try:
        report = BRAIN_LOGS_DIR / "backup_restore_drill.json"
        if not report.exists():
            return 999.0
        data = json.loads(report.read_text())
        if not data.get("all_ok"):
            return 999.0
        ts_s = data.get("finished_at") or data.get("started_at")
        if not ts_s:
            return 999.0
        ts = datetime.fromisoformat(str(ts_s).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return round((time.time() - ts.timestamp()) / 3600.0, 2)
    except Exception as exc:
        log.debug("backup_restore_drill_age measurement failed: %s", exc)
        return 999.0


def _measure_logs_dir_growth_24h_mb() -> float:
    """24-hour delta in logs/ size, using snapshots written by run_wal_checkpoint.

    Reads the bounded snapshot history from brain_config_store
    (LOGS_DIR_HISTORY_KEY, max 14 entries), pairs the latest with the
    snapshot closest to 24h prior, returns the delta in MB. Returns 0.0
    when there is no usable 24h-ago baseline (cold start) — the absolute
    `logs_dir_total_mb` SLO is the safety net while history accumulates.

    Memory: O(14) entries in memory during the measurement, immediately
    released. Performance: single brain_config_store read + linear scan
    over ≤14 entries.
    """
    try:
        import brain_config_store
        from db_maintenance import LOGS_DIR_HISTORY_KEY

        raw = brain_config_store.get(LOGS_DIR_HISTORY_KEY)
        if not raw:
            return 0.0
        history = json.loads(raw)
        if not isinstance(history, list) or len(history) < 2:
            return 0.0
        # newest entry's mb minus the closest-to-24h-ago entry's mb
        latest = history[-1]
        try:
            latest_mb = float(latest.get("mb", 0.0))
        except (TypeError, ValueError):
            latest_mb = 0.0
        if latest_mb <= 0.0:
            return 0.0
        latest_ts = datetime.fromisoformat(str(latest["ts"]).replace("Z", "+00:00"))
        target_ts = latest_ts - timedelta(hours=24)
        baseline = None
        best_gap: float | None = None
        # Baseline must have a real measurement (mb > 0). Cold-start or
        # bug-injected mb=0 rows would otherwise produce a false 24h delta
        # equal to the entire current logs/ size.
        for entry in history[:-1]:
            try:
                entry_mb = float(entry.get("mb", 0.0))
            except (TypeError, ValueError):
                continue
            if entry_mb <= 0.0:
                continue
            try:
                ts = datetime.fromisoformat(str(entry["ts"]).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            gap = abs((ts - target_ts).total_seconds())
            if best_gap is None or gap < best_gap:
                best_gap = gap
                baseline = entry
        # Require the baseline to be at least 18h old AND no older than 36h to
        # avoid comparing today's snapshot to a 2-day-old one (which would
        # double the apparent delta) or to a 4-hour-old one (which would
        # under-report).
        if not baseline or best_gap is None:
            return 0.0
        if best_gap > 18 * 3600:  # too far from 24h-ago
            return 0.0
        try:
            baseline_mb = float(baseline.get("mb", 0.0))
        except (TypeError, ValueError):
            return 0.0
        if baseline_mb <= 0.0:
            return 0.0
        delta = latest_mb - baseline_mb
        return round(delta, 1)
    except Exception as exc:
        log.debug("logs_dir_growth_24h_mb measurement failed: %s", exc)
        return 0.0


def _measure_logs_dir_total_mb() -> float:
    """Sum size of all files under brain logs/ directory."""
    try:
        total = 0
        for p in BRAIN_LOGS_DIR.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    continue
        return round(total / (1024 * 1024), 1)
    except Exception as exc:
        log.debug("logs_dir_total_mb measurement failed: %s", exc)
        return 0.0


def _measure_entry_contract_missing_pct() -> float:
    try:
        from entry_contract_audit import audit_collections

        result = audit_collections()
        return float(result.get("missing_pct") or 0.0)
    except Exception as exc:
        log.debug("entry_contract_missing_pct measurement failed: %s", exc)
        return 0.0


def _measure_telegram_backlog_pending_count() -> float:
    try:
        if not AUTONOMY_DB.exists():
            return 0.0
        with sqlite3.connect(str(AUTONOMY_DB)) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM llm_backlog WHERE kind='telegram' AND status='pending'"
            ).fetchone()
        return float(row[0]) if row else 0.0
    except sqlite3.Error as exc:
        # Fresh installs may not have llm_backlog yet.
        log.debug("telegram_backlog_pending_count measurement failed: %s", exc)
        return 0.0


def _measure_telegram_direct_health() -> float:
    try:
        from telegram_alert import direct_api_healthcheck

        ok, reason = direct_api_healthcheck()
        if not ok:
            log.warning("telegram direct healthcheck failed: %s", reason)
        return 0.0 if ok else 1.0
    except Exception as exc:
        log.debug("telegram_direct_health measurement failed: %s", exc)
        return 1.0


def _measure_openclaw_gateway_health() -> float:
    """Return 0 when the local OpenClaw gateway port accepts TCP, else 1.

    Keep this probe deliberately cheap and side-effect-free. The deeper CLI
    ``openclaw gateway status`` command is useful for operators, but the SLO
    path runs every few minutes and should not spawn Node/Codex subprocesses or
    compete with the agent-dispatch path it is protecting.
    """

    try:
        with socket.create_connection(("127.0.0.1", 18789), timeout=1.0):
            return 0.0
    except OSError as exc:
        log.warning("openclaw gateway healthcheck failed: %s", exc)
        return 1.0


def _measure_task_dispatch_stale_started_count() -> float:
    """Count dispatch attempts that started but never closed."""

    try:
        cutoff = datetime.now(UTC) - timedelta(minutes=15)
        with sqlite3.connect(str(AUTONOMY_DB)) as conn:
            row = conn.execute(
                """SELECT COUNT(*) FROM task_dispatch_attempts
                   WHERE status = 'started' AND started_at <= ?""",
                (cutoff.isoformat(timespec="seconds"),),
            ).fetchone()
        return float(row[0]) if row else 0.0
    except sqlite3.Error as exc:
        log.debug("task_dispatch_stale_started_count measurement failed: %s", exc)
        return 0.0


def _measure_task_failure_lesson_missing_count() -> float:
    """Count closed failure/deferred attempts whose Reflexion lesson is absent."""

    try:
        cutoff = datetime.now(UTC) - timedelta(minutes=15)
        with sqlite3.connect(str(AUTONOMY_DB)) as conn:
            rows = conn.execute(
                """SELECT metadata FROM task_dispatch_attempts
                   WHERE status IN ('failed', 'deferred') AND completed_at <= ?""",
                (cutoff.isoformat(timespec="seconds"),),
            ).fetchall()
        missing = 0
        for (raw_meta,) in rows:
            try:
                meta = json.loads(raw_meta or "{}")
            except (json.JSONDecodeError, TypeError):
                meta = {}
            if not isinstance(meta, dict):
                meta = {}
            status = str(meta.get("failure_lesson_status") or "")
            if status != "recorded":
                missing += 1
        return float(missing)
    except sqlite3.Error as exc:
        log.debug("task_failure_lesson_missing_count measurement failed: %s", exc)
        return 0.0


def _measure_autonomous_work_visibility_gap_count() -> float:
    """Count autonomous/background work records missing trace evidence."""

    try:
        from autonomous_work import visibility_gap_count

        return float(visibility_gap_count(hours=24))
    except Exception as exc:
        log.debug("autonomous_work_visibility_gap_count measurement failed: %s", exc)
        return 0.0


def _measure_self_eval_drift_7d() -> float:
    """Read the latest self_eval drift_pct from brain_config_store.

    Non-zero until the nightly self_eval drive has run at least once;
    returns 0 on missing/invalid data so the gauge stays silent at startup.
    """
    try:
        import brain_config_store

        raw = brain_config_store.get("self_eval.drift_7d")
        if not raw:
            return 0.0
        data = json.loads(raw)
        return float(data.get("drift_pct", 0.0))
    except Exception:
        return 0.0


def _measure_boot_context_degraded_1h() -> float:
    """Count degraded boot-context serves in the last hour.

    Reads /Users/chrischo/server/brain/logs/degraded_serves.log, which
    claude_boot.sh appends to on every cache-fallback or unreachable-brain
    event. 0 = healthy; any sustained non-zero means Chris's sessions are
    starting without fresh brain context and no one would otherwise notice.
    """
    try:
        import datetime as _dt

        log_file = BRAIN_LOGS_DIR / "degraded_serves.log"
        if not log_file.exists():
            return 0.0
        cutoff = _dt.datetime.now(_dt.UTC) - _dt.timedelta(hours=1)
        count = 0
        with log_file.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                ts_str = line.split("\t", 1)[0]
                try:
                    ts = _dt.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=_dt.UTC)
                except (ValueError, TypeError):
                    continue
                if ts >= cutoff:
                    count += 1
        return float(count)
    except Exception as exc:
        log.debug("boot_context_degraded_1h measurement failed: %s", exc)
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


def _rss_kb_for_pid(pid: int) -> int:
    """Return current ps-reported RSS in KiB for pid, or 0 if unavailable."""

    if pid <= 0:
        return 0
    rss_out = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(pid)],
        capture_output=True,
        text=True,
        timeout=2,
    )
    try:
        return int((rss_out.stdout or "").strip().split()[0])
    except (IndexError, ValueError):
        return 0


def _brain_server_rss_kb_from_process_table() -> int:
    """Find the real long-running FastAPI server and return its current RSS.

    Do not use `pgrep -f "brain/server.py"` here: the SLO runner itself can
    contain that literal in its command text, and pgrep may return the short-
    lived checker before the actual server. Parse `ps` and require a Python
    command whose argument vector includes the repo's exact `server.py` path.
    """

    server_path = str((BRAIN_DIR / "server.py").resolve())
    ps_out = subprocess.run(
        ["ps", "-axo", "pid=,rss=,command="],
        capture_output=True,
        text=True,
        timeout=3,
    )
    best_rss = 0
    for line in (ps_out.stdout or "").splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            rss_kb = int(parts[1])
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        cmd = parts[2]
        try:
            argv = shlex.split(cmd)
        except ValueError:
            argv = cmd.split()
        if len(argv) < 2:
            continue
        exe = Path(argv[0]).name.lower()
        if "python" not in exe:
            continue
        if server_path not in argv[1:]:
            continue
        best_rss = max(best_rss, rss_kb)
    return best_rss


def _measure_brain_server_rss_mb() -> float:
    """RSS in MB of the brain server process.

    In-process: reads current RSS for this pid via ps.
    Out-of-process (scheduler subprocess / CLI): parses the process table and
    selects only the Python process running this repo's exact server.py path.

    Returns 0.0 on failure so the SLO stays silent rather than firing
    a spurious breach at 0 < 3072.
    """
    try:
        server_path = (BRAIN_DIR / "server.py").resolve()
        argv0 = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
        kb = _rss_kb_for_pid(os.getpid()) if argv0 == server_path else 0
        if kb <= 0:
            kb = _brain_server_rss_kb_from_process_table()
        return round(kb / 1024.0, 1) if kb > 0 else 0.0
    except Exception as exc:
        log.warning("brain_server_rss_mb measurement failed: %s", exc)
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
    "logs_dir_total_mb": _measure_logs_dir_total_mb,
    "logs_dir_growth_24h_mb": _measure_logs_dir_growth_24h_mb,
    "entry_contract_missing_pct": _measure_entry_contract_missing_pct,
    "telegram_backlog_pending_count": _measure_telegram_backlog_pending_count,
    "telegram_direct_health": _measure_telegram_direct_health,
    "openclaw_gateway_health": _measure_openclaw_gateway_health,
    "task_dispatch_stale_started_count": _measure_task_dispatch_stale_started_count,
    "task_failure_lesson_missing_count": _measure_task_failure_lesson_missing_count,
    "autonomous_work_visibility_gap_count": _measure_autonomous_work_visibility_gap_count,
    "boot_context_degraded_1h": _measure_boot_context_degraded_1h,
    "self_eval_drift_7d": _measure_self_eval_drift_7d,
    "qdrant_backup_age_hours": _measure_qdrant_backup_age_hours,
    "neo4j_backup_age_hours": _measure_neo4j_backup_age_hours,
    "backup_restore_drill_age_hours": _measure_backup_restore_drill_age_hours,
    "brain_server_rss_mb": _measure_brain_server_rss_mb,
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


def _alert_rate_limit_s(slo: SLO) -> int:
    """Return the Telegram alert suppression window for this SLO.

    `sleep_consolidate` is a once-daily job. Its SLO can remain breached for
    the full 24h measurement window after a single slow cycle, so the default
    30-minute alert cadence is pure noise. Keep the SLO visible in `/brain/slos`
    but page at most once per daily cycle.
    """

    if slo.name == "sleep_cycles_duration_1d_p95":
        return DAILY_JOB_ALERT_RATE_LIMIT_S
    return ALERT_RATE_LIMIT_S


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
        # (22:30-07:30 PT). The prior 23:00/07:00 fallbacks silently narrowed
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
    if now - last_at < _alert_rate_limit_s(result.slo):
        return False
    sent = _alert_telegram(result.slo, result.actual)
    if sent:
        _save_last_alert_at(result.slo.name, result.slo.severity, now)
    return sent


def run() -> dict:
    """Scheduler entrypoint: check all SLOs, dispatch alerts on breach.

    Returns a summary suitable for /metrics consumption.
    """
    results = check_all()
    breached = [r for r in results if r.breached]
    remediation = {"actions": []}
    if breached:
        try:
            from slo_remediation import apply_direct_remediations

            remediation = apply_direct_remediations(
                [
                    {
                        "slo": r.slo.name,
                        "current": r.actual,
                        "target": r.slo.target,
                        "severity": r.slo.severity,
                    }
                    for r in breached
                ]
            )
        except Exception as exc:
            log.warning("SLO direct remediation failed: %s", exc)
            remediation = {"error": str(exc)[:300], "actions": []}
    alerts_sent = 0
    for r in breached:
        if maybe_alert(r):
            alerts_sent += 1
    return {
        "checked": len(results),
        "breached": len(breached),
        "alerts_sent": alerts_sent,
        "remediation": remediation,
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
