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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HISTOGRAM_WINDOW = 512


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._routes: dict[str, RouteStats] = defaultdict(RouteStats)
        self._jobs: dict[str, JobStats] = defaultdict(JobStats)
        self._dispatch = DispatchStats()
        self._memory_writes: deque = deque(maxlen=1000)  # timestamps
        self._started_at = _now_iso()
        self._last_learn_success_at: str = ""
        self._last_backup_at: str = ""
        self._last_backup_ok: bool = True
        self._search_latencies: list[int] = []

    def record_search_latency(self, total_ms: int, source_timings: dict[str, int] | None = None) -> None:
        """Record search latency for rolling p50/p95/p99 tracking."""
        with self._lock:
            self._search_latencies.append(total_ms)
            if len(self._search_latencies) > 1000:
                self._search_latencies = self._search_latencies[-1000:]

    def search_latency_stats(self) -> dict:
        """Return p50/p95/p99 search latency from rolling window."""
        with self._lock:
            latencies = getattr(self, '_search_latencies', [])
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

    def record_request(self, path: str, latency_ms: float, error: bool = False) -> None:
        with self._lock:
            key = self._normalize_path(path)
            stats = self._routes[key]
            stats.count += 1
            stats.latencies_ms.append(latency_ms)
            if error:
                stats.errors += 1

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

    def record_dispatch(self, ok: bool, duration_ms: int, rate_limited: bool, auth_failed: bool, attempts: int) -> None:
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
                    round(self._dispatch.total_duration_ms / max(1, self._dispatch.successes + self._dispatch.failures), 1)
                ),
            }

            return {
                "started_at": self._started_at,
                "routes": routes,
                "jobs": jobs,
                "memory_writes_1h": memory_writes_1h,
                "dispatch": dispatch,
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
        conn.execute("INSERT INTO metrics_snapshots (timestamp, payload) VALUES (?, ?)",
                     (datetime.now(timezone.utc).isoformat(), json.dumps(self.snapshot())))
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
