"""brain_core/metrics_buffer.py — in-process metrics sink for the brain API.

Lightweight counters + ring-buffer histograms that server.py updates as
requests flow through. Exposed via GET /metrics so the UI dashboard and
Glance panel can read operational state without touching disk/logs.

Tracked:
  * Per-route request counts + p50/p95 latency (rolling 512-sample window)
  * Per-job execution history (last success, consecutive failures, last error)
  * Memory-write rate (sliding 1h count)
  * Dispatch attempts (openclaw retries, rate-limit hits, auth failures)

Zero external deps. Thread-safe via a single lock. Reset on server restart.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HISTOGRAM_WINDOW = 512


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class RouteStats:
    count: int = 0
    errors: int = 0
    latencies_ms: deque = field(default_factory=lambda: deque(maxlen=HISTOGRAM_WINDOW))

    def percentile(self, p: float) -> float:
        if not self.latencies_ms:
            return 0.0
        sorted_lat = sorted(self.latencies_ms)
        idx = min(len(sorted_lat) - 1, int(len(sorted_lat) * p))
        return round(sorted_lat[idx], 2)


@dataclass
class PhaseStats:
    """Rolling latency window for a single /recall pipeline phase.

    Populated from the source_timings dict the server handler passes into
    record_search_latency. Each phase is an independent rolling window so
    p50/p95/p99 can be queried without decoding full request payloads.
    """

    count: int = 0
    latencies_ms: deque = field(default_factory=lambda: deque(maxlen=HISTOGRAM_WINDOW))

    def percentile(self, p: float) -> float:
        if not self.latencies_ms:
            return 0.0
        sorted_lat = sorted(self.latencies_ms)
        idx = min(len(sorted_lat) - 1, int(len(sorted_lat) * p))
        return round(float(sorted_lat[idx]), 2)


@dataclass
class JobStats:
    last_success_at: str = ""
    last_failure_at: str = ""
    last_error: str = ""
    success_count: int = 0
    failure_count: int = 0
    consecutive_failures: int = 0


@dataclass
class DispatchStats:
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    rate_limited: int = 0
    auth_failed: int = 0
    total_duration_ms: int = 0


class MetricsBuffer:
    # Phase keys we track from the /recall/v2 timing dict. Unknown integer
    # keys with an _ms suffix are also captured (forward-compatible).
    _PHASE_KEYS = frozenset(
        {
            "search_ms",
            "expansion_ms",
            "hyde_ms",
            "rag_ms",
            "canonical_ms",
            "obsidian_ms",
            "graph_ms",
            "fts_ms",
            "graph_prefetch_ms",
            "rrf_ms",
            "rerank_ms",
            "cross_encoder_ms",
            "decay_ms",
            "enrich_ms",
        }
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._routes: dict[str, RouteStats] = defaultdict(RouteStats)
        self._phases: dict[str, PhaseStats] = defaultdict(PhaseStats)
        self._jobs: dict[str, JobStats] = defaultdict(JobStats)
        self._dispatch = DispatchStats()
        self._memory_writes: deque = deque(maxlen=1000)  # timestamps
        self._started_at = _now_iso()
        self._last_learn_success_at: str = ""
        self._last_backup_at: str = ""
        self._last_backup_ok: bool = True
        self._search_latencies: deque = deque(maxlen=1000)
        # 2026-04-17 hook adoption metrics — counts + p95 latency per-hook
        # per-agent, exposed in /metrics so we can verify OpenClaw's
        # brain-active-recall hook actually fires across all 5 agents.
        self._hook_calls: dict[tuple[str, str], int] = defaultdict(int)
        self._hook_latencies: dict[str, deque] = defaultdict(lambda: deque(maxlen=500))

    def record_search_latency(self, total_ms: int, source_timings: dict[str, int] | None = None) -> None:
        """Record search latency and per-phase timings for p50/p95/p99 tracking."""
        with self._lock:
            self._search_latencies.append(total_ms)
            if not source_timings:
                return
            for key, value in source_timings.items():
                if not isinstance(key, str) or not key.endswith("_ms"):
                    continue
                if key not in self._PHASE_KEYS:
                    continue
                try:
                    ms = float(value)
                except (TypeError, ValueError):
                    continue
                phase = self._phases[key]
                phase.count += 1
                phase.latencies_ms.append(ms)

    def search_latency_stats(self) -> dict:
        """Return p50/p95/p99 search latency from rolling window."""
        with self._lock:
            latencies = list(self._search_latencies)
            if not latencies:
                return {"p50": 0, "p95": 0, "p99": 0, "count": 0}
            s = sorted(latencies)
            n = len(s)
            return {
                "p50": s[n // 2],
                "p95": s[min(n - 1, int(n * 0.95))],
                "p99": s[min(n - 1, int(n * 0.99))],
                "count": n,
            }

    def phase_latency_stats(self) -> dict[str, dict[str, float]]:
        """Return per-phase p50/p95/p99 from rolling windows."""
        with self._lock:
            return {
                key: {
                    "count": stats.count,
                    "p50_ms": stats.percentile(0.50),
                    "p95_ms": stats.percentile(0.95),
                    "p99_ms": stats.percentile(0.99),
                }
                for key, stats in self._phases.items()
                if stats.count > 0
            }

    def record_request(
        self,
        path: str,
        latency_ms: float,
        error: bool = False,
        status_code: int = 0,
    ) -> None:
        """Record a completed request. 2026-04-16 R-8: status_code param
        added so metrics can distinguish 2xx/4xx/5xx — previously every
        non-5xx was indistinguishable in aggregate stats."""
        with self._lock:
            key = self._normalize_path(path)
            stats = self._routes[key]
            stats.count += 1
            stats.latencies_ms.append(latency_ms)
            if error:
                stats.errors += 1
            # Per-status-band counters. Stored as attributes so legacy
            # readers of stats don't break; new readers can access them.
            if status_code:
                band = status_code // 100
                attr = f"status_{band}xx_count"
                setattr(stats, attr, getattr(stats, attr, 0) + 1)

    def record_job_result(self, job_name: str, ok: bool, error: str = "") -> None:
        with self._lock:
            stats = self._jobs[job_name]
            if ok:
                stats.last_success_at = _now_iso()
                stats.success_count += 1
                stats.consecutive_failures = 0
            else:
                stats.last_failure_at = _now_iso()
                stats.failure_count += 1
                stats.consecutive_failures += 1
                stats.last_error = error[:200]

    def record_memory_write(self) -> None:
        with self._lock:
            self._memory_writes.append(time.time())

    def record_learn_success(self) -> None:
        with self._lock:
            self._last_learn_success_at = _now_iso()

    def record_backup_result(self, ok: bool) -> None:
        with self._lock:
            self._last_backup_at = _now_iso()
            self._last_backup_ok = ok

    def record_hook_call(self, hook_name: str, agent: str) -> None:
        """Track per-agent hook invocations (brain-active-recall adoption check)."""
        with self._lock:
            self._hook_calls[(hook_name, agent)] += 1

    def record_hook_latency(self, hook_name: str, duration_ms: int) -> None:
        with self._lock:
            self._hook_latencies[hook_name].append(duration_ms)

    def record_dispatch(
        self, ok: bool, duration_ms: int, rate_limited: bool, auth_failed: bool, attempts: int
    ) -> None:
        with self._lock:
            self._dispatch.attempts += attempts
            if ok:
                self._dispatch.successes += 1
            else:
                self._dispatch.failures += 1
            if rate_limited:
                self._dispatch.rate_limited += 1
            if auth_failed:
                self._dispatch.auth_failed += 1
            self._dispatch.total_duration_ms += duration_ms

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            one_hour_ago = now - 3600
            # Count memory writes in the last hour
            while self._memory_writes and self._memory_writes[0] < one_hour_ago:
                self._memory_writes.popleft()
            memory_writes_1h = len(self._memory_writes)

            routes = {
                path: {
                    "count": s.count,
                    "errors": s.errors,
                    "p50_ms": s.percentile(0.50),
                    "p95_ms": s.percentile(0.95),
                    "p99_ms": s.percentile(0.99),
                }
                for path, s in self._routes.items()
            }

            jobs = {
                name: {
                    "last_success_at": s.last_success_at,
                    "last_failure_at": s.last_failure_at,
                    "last_error": s.last_error,
                    "success_count": s.success_count,
                    "failure_count": s.failure_count,
                    "consecutive_failures": s.consecutive_failures,
                }
                for name, s in self._jobs.items()
            }

            dispatch = {
                "attempts": self._dispatch.attempts,
                "successes": self._dispatch.successes,
                "failures": self._dispatch.failures,
                "rate_limited": self._dispatch.rate_limited,
                "auth_failed": self._dispatch.auth_failed,
                "mean_duration_ms": (
                    round(
                        self._dispatch.total_duration_ms
                        / max(1, self._dispatch.successes + self._dispatch.failures),
                        1,
                    )
                ),
            }

            phase_latency = {
                key: {
                    "count": stats.count,
                    "p50_ms": stats.percentile(0.50),
                    "p95_ms": stats.percentile(0.95),
                    "p99_ms": stats.percentile(0.99),
                }
                for key, stats in self._phases.items()
                if stats.count > 0
            }

            # 2026-04-17 hook adoption: group per-hook per-agent counts +
            # per-hook latency percentiles. Lets /metrics consumers verify
            # brain-active-recall is firing for every OpenClaw agent, not
            # just Claude Code.
            hook_adoption: dict[str, Any] = {}
            for (hook, agent), n in self._hook_calls.items():
                hook_adoption.setdefault(hook, {"per_agent": {}, "total": 0})
                hook_adoption[hook]["per_agent"][agent] = n
                hook_adoption[hook]["total"] += n
            for hook, lats in self._hook_latencies.items():
                if not lats:
                    continue
                sorted_lats = sorted(lats)
                p50 = sorted_lats[len(sorted_lats) // 2]
                p95 = sorted_lats[min(len(sorted_lats) - 1, int(len(sorted_lats) * 0.95))]
                hook_adoption.setdefault(hook, {"per_agent": {}, "total": 0})
                hook_adoption[hook]["p50_ms"] = p50
                hook_adoption[hook]["p95_ms"] = p95

            return {
                "started_at": self._started_at,
                "routes": routes,
                "phase_latency": phase_latency,
                "jobs": jobs,
                "memory_writes_1h": memory_writes_1h,
                "dispatch": dispatch,
                "hook_adoption": hook_adoption,
                "last_learn_success_at": self._last_learn_success_at,
                "last_backup_at": self._last_backup_at,
                "last_backup_ok": self._last_backup_ok,
            }

    def persist_to_sqlite(self, db_path: str) -> None:
        """Save current metrics snapshot to SQLite for cross-restart persistence."""
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        conn.execute("""CREATE TABLE IF NOT EXISTS metrics_snapshots (
            id INTEGER PRIMARY KEY, timestamp TEXT, payload TEXT)""")
        conn.execute(
            "INSERT INTO metrics_snapshots (timestamp, payload) VALUES (?, ?)",
            (datetime.now(UTC).isoformat(), json.dumps(self.snapshot())),
        )
        # Prune >90 days
        conn.execute("DELETE FROM metrics_snapshots WHERE timestamp < datetime('now', '-90 days')")
        conn.close()

    def load_from_sqlite(self, db_path: str) -> None:
        """Restore route/job counters from most recent snapshot."""
        if not Path(db_path).exists():
            return
        try:
            conn = sqlite3.connect(str(db_path))
            row = conn.execute("SELECT payload FROM metrics_snapshots ORDER BY id DESC LIMIT 1").fetchone()
            conn.close()
            if not row:
                return
            data = json.loads(row[0])
            # Restore job stats as proper JobStats objects
            with self._lock:
                for name, stats_dict in data.get("jobs", {}).items():
                    js = JobStats()
                    if isinstance(stats_dict, dict):
                        js.last_success_at = stats_dict.get("last_success_at", "")
                        js.last_failure_at = stats_dict.get("last_failure_at", "")
                        js.last_error = stats_dict.get("last_error", "")
                        js.success_count = stats_dict.get("success_count", 0)
                        js.failure_count = stats_dict.get("failure_count", 0)
                        js.consecutive_failures = stats_dict.get("consecutive_failures", 0)
                    self._jobs[name] = js
        except Exception:
            pass

    @staticmethod
    def _normalize_path(path: str) -> str:
        """Collapse path parameters so /memory/abc123 → /memory/{id}."""
        import re

        path = re.sub(r"/memory/[^/]+$", "/memory/{id}", path)
        path = re.sub(r"/memory/contradictions/[^/]+/resolve$", "/memory/contradictions/{id}/resolve", path)
        path = re.sub(r"/boot-context/[^/]+$", "/boot-context/{agent}", path)
        path = re.sub(r"/profile/section/[^/]+$", "/profile/section/{name}", path)
        path = re.sub(r"/capture/[^/]+$", "/capture/{type}", path)
        path = re.sub(r"/jobs/[^/]+/history$", "/jobs/{name}/history", path)
        path = re.sub(r"/jobs/[^/]+$", "/jobs/{name}", path)
        return path.split("?")[0]


# Module singleton
metrics_buffer = MetricsBuffer()
