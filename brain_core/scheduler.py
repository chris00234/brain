"""brain_core/scheduler.py — the brain's own cron.

Replaces 15 launchd plists with an AsyncIOScheduler that runs inside the
FastAPI event loop. Jobs execute as subprocess fire-and-forget (same semantics
as the POST /jobs/{name} route, which this scheduler reuses) so a long-running
ingest never blocks the server's request handlers.

Why in-process (and not launchd)?
  - No Python cold start per cron tick (brain_core modules stay hot)
  - One place to see job state (/jobs endpoints)
  - Cron edits are a Python constant, not a plist reload
  - Job dependencies can be expressed in code

Jobs are defined declaratively in JOB_SCHEDULE below. Each entry maps to a job
in server.py's JOB_REGISTRY, so the scheduler is just a cron → POST /jobs/{name}
bridge. No business logic lives here.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

log = logging.getLogger("brain.scheduler")


# 2026-04-17: ScheduledJob + JOB_SCHEDULE both live in job_definitions
# now. We re-export here so `from scheduler import ScheduledJob,
# JOB_SCHEDULE` keeps working. Previously ScheduledJob was defined in
# this file and job_definitions imported back — creating a circular
# import that broke when job_definitions was imported standalone
# (caught by tests/unit/test_brain_core_smoke.py).
from job_definitions import JOB_SCHEDULE, ScheduledJob  # noqa: E402, F401

# Historical inline entries removed (see job_definitions.py):
# The inline list used to span ~874 lines here.

JOB_BY_NAME = {job.name: job for job in JOB_SCHEDULE}


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError:
        return default


class BrainScheduler:
    """Wraps APScheduler. Each job triggers a registered command in the brain.

    The command dispatcher is passed in at start() time so this module stays
    free of any server.py import (avoids circular dependency).
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._scheduler = AsyncIOScheduler(timezone="America/Los_Angeles")
        self._dispatcher: Callable[[str], int] | None = None
        self._history: dict[str, list[dict]] = {}
        self._running_jobs: dict[str, int] = {}  # job_name -> pid
        self._MAX_HISTORY = 20
        self._alerted_jobs: set[str] = set()
        self._pending_completions: dict[str, tuple[float, int | None]] = {}  # job_name -> (start_ts, row_id)
        self._state_lock = threading.RLock()
        self._tick_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="brain_tick")
        self._tick_future: Future | None = None
        self._resource_limits = {
            "heavy": _env_int("BRAIN_SCHED_MAX_HEAVY_JOBS", 1),
            "llm": _env_int("BRAIN_SCHED_MAX_LLM_JOBS", 1),
            "embedder": _env_int(
                "BRAIN_SCHED_MAX_EMBEDDER_JOBS",
                _env_int("BRAIN_SCHED_MAX_OLLAMA_JOBS", 1),
            ),
            "index": _env_int("BRAIN_SCHED_MAX_INDEX_JOBS", 1),
        }
        self._resource_defer_s = _env_int("BRAIN_SCHED_RESOURCE_DEFER_S", 300)
        self._resource_defers: dict[str, dict] = {}
        # 2026-04-22: db_path injectable so tests don't leak test_job/stale_job
        # rows into the prod scheduler_history.db (72 ghost rows found in 24h
        # metrics before this change).
        self._db_path = db_path or Path(__file__).resolve().parent.parent / "logs" / "scheduler_history.db"
        self._load_history_from_db()

    def _load_history_from_db(self) -> None:
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._db_path))
            conn.execute("""CREATE TABLE IF NOT EXISTS job_history (
                id INTEGER PRIMARY KEY, job_name TEXT, started_at TEXT,
                pid INTEGER, error TEXT, manual INTEGER DEFAULT 0,
                finished_at TEXT DEFAULT NULL, duration_ms INTEGER DEFAULT NULL)""")
            # Migrate existing databases missing new columns
            for col, typedef in [
                ("finished_at", "TEXT DEFAULT NULL"),
                ("duration_ms", "INTEGER DEFAULT NULL"),
            ]:
                with contextlib.suppress(sqlite3.OperationalError):
                    conn.execute(f"ALTER TABLE job_history ADD COLUMN {col} {typedef}")
            cur = conn.execute(
                "SELECT job_name, started_at, pid, error, manual, finished_at, duration_ms "
                "FROM job_history ORDER BY id DESC LIMIT 400"
            )
            for name, started, pid, error, manual, finished, duration in cur.fetchall():
                entry = {
                    "started_at": started,
                    "pid": pid,
                    "error": error,
                    "finished_at": finished,
                    "duration_ms": duration,
                }
                if manual:
                    entry["manual"] = True
                history = self._history.setdefault(name, [])
                history.insert(0, entry)
            for name in self._history:
                self._history[name] = self._history[name][: self._MAX_HISTORY]
            conn.close()
        except Exception as exc:
            log.debug("scheduler history load failed: %s", exc)

    def _persist_entry(self, job_name: str, entry: dict) -> int | None:
        """Insert a history row. Returns the row id (used to update on completion)."""
        try:
            conn = sqlite3.connect(str(self._db_path))
            cur = conn.execute(
                "INSERT INTO job_history (job_name, started_at, pid, error, manual) VALUES (?, ?, ?, ?, ?)",
                (
                    job_name,
                    entry.get("started_at"),
                    entry.get("pid", -1),
                    entry.get("error"),
                    1 if entry.get("manual") else 0,
                ),
            )
            row_id = cur.lastrowid
            conn.commit()
            conn.close()
            return row_id
        except Exception:
            return None

    def record_completion(
        self, job_name: str, row_id: int | None, start_ts: float, error: str | None = None
    ) -> None:
        """Called by _wait_for_job after a subprocess finishes."""
        finished_at = datetime.now(UTC).isoformat()
        duration_ms = int((time.time() - start_ts) * 1000)

        # Update in-memory history (find the matching entry by row_id or last unfinished)
        for entry in reversed(self._history.get(job_name, [])):
            if entry.get("finished_at") is None:
                entry["finished_at"] = finished_at
                entry["duration_ms"] = duration_ms
                if error and not entry.get("error"):
                    entry["error"] = error[:200]
                break

        # Update SQLite row
        if row_id is not None:
            try:
                conn = sqlite3.connect(str(self._db_path))
                conn.execute(
                    "UPDATE job_history SET finished_at=?, duration_ms=?, error=COALESCE(error, ?) WHERE id=?",
                    (finished_at, duration_ms, error[:200] if error else None, row_id),
                )
                conn.commit()
                conn.close()
            except Exception as exc:
                log.debug("scheduler completion persist failed for %s: %s", job_name, exc)

    def _reconcile_orphans(self) -> int:
        """2026-04-17 reindex-silent-death fix: on server startup, reconcile
        `job_history` rows that were left with finished_at=NULL by the prior
        brain-server instance. Those rows are orphans — their `_wait_for_job`
        thread died with the old process, so completion never recorded.

        Two classes of orphans:
          1. PID still alive → process survived restart (subprocess was
             detached via start_new_session=True). Keep the row but do nothing
             yet; the next reaper tick will catch it when the process exits.
          2. PID gone → the subprocess also died. Mark the row as completed
             with an 'orphaned_by_restart' error so UI / SLO stop showing it
             as "running forever".

        Returns the number of rows reconciled.
        """
        n_reconciled = 0
        try:
            conn = sqlite3.connect(str(self._db_path))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, job_name, pid, started_at FROM job_history " "WHERE finished_at IS NULL"
            ).fetchall()
            for row in rows:
                pid = row["pid"] or -1
                alive = False
                if pid > 0:
                    try:
                        os.kill(pid, 0)
                        alive = True
                    except (ProcessLookupError, PermissionError):
                        alive = False
                if alive:
                    # Subprocess survived the restart — rebuild tracking so
                    # the reaper can catch its eventual exit.
                    try:
                        started_at = row["started_at"]
                        if started_at and "T" in started_at:
                            start_ts = datetime.fromisoformat(started_at).timestamp()
                        else:
                            start_ts = time.time() - 60.0
                    except Exception:
                        start_ts = time.time() - 60.0
                    self._running_jobs[row["job_name"]] = pid
                    self._pending_completions[row["job_name"]] = (start_ts, row["id"])
                    continue
                # Dead — record completion with orphan marker
                finished_at = datetime.now(UTC).isoformat()
                conn.execute(
                    "UPDATE job_history SET finished_at=?, error=COALESCE(error, ?) WHERE id=?",
                    (finished_at, "orphaned_by_restart", row["id"]),
                )
                n_reconciled += 1
            conn.commit()
            conn.close()
        except Exception as exc:
            log.warning("orphan reconcile failed: %s", exc)
        if n_reconciled:
            log.info("reconciled %d orphaned job rows from prior brain-server instance", n_reconciled)
        return n_reconciled

    def start(self, dispatcher: Callable[[str], int]) -> None:
        """Start the scheduler with a job dispatcher callback.

        dispatcher(job_name) -> pid  — called when a cron fires, same contract
        as the existing POST /jobs/{name} route handler.
        """
        self._dispatcher = dispatcher
        # 2026-04-17: reconcile orphans left by previous brain-server instance.
        # Must run before we start adding scheduler jobs — otherwise a freshly
        # fired cron could collide with a "running" orphan row and the dedup
        # in _dispatch_job (check for existing _running_jobs[name]) would fail
        # because that in-memory state is rebuilt from SQLite orphans first.
        self._reconcile_orphans()
        for job in JOB_SCHEDULE:
            self._scheduler.add_job(
                self._fire,
                trigger=job.trigger,
                id=job.name,
                args=[job.name],
                name=job.description,
                replace_existing=True,
                misfire_grace_time=job.misfire_grace,
                coalesce=True,  # collapse missed runs into 1
            )
        # In-process task executor (runs every 30s). _tick_executor itself is
        # fire-and-forget: it hands work to self._tick_pool and returns in
        # <1 ms, so a stalled escalation inside task_queue.process_pending
        # can no longer pile up "max_instances reached" skips on the
        # scheduler. See server.err.log 2026-04-20 17:31-17:34 for the
        # original stall pattern.
        self._scheduler.add_job(
            self._tick_executor,
            trigger=IntervalTrigger(seconds=30),
            id="task_executor",
            name="Task executor tick (30s, in-process)",
            replace_existing=True,
            misfire_grace_time=60,
            coalesce=True,
        )
        # 2026-04-16 fix: completion reaper. Previously _pending_completions
        # was populated at dispatch but never drained — the missing
        # _wait_for_job docstring-referenced method was never implemented.
        # Result: finished_at/duration_ms stayed NULL for every scheduled
        # run and the dict grew unbounded. This 15s-interval reaper polls
        # each PID with kill(0); dead processes get record_completion +
        # removed from _running_jobs and _pending_completions.
        self._scheduler.add_job(
            self._reap_completions,
            trigger=IntervalTrigger(seconds=15),
            id="completion_reaper",
            name="Scheduler completion reaper (15s, in-process)",
            replace_existing=True,
            misfire_grace_time=30,
            coalesce=True,
        )
        self._scheduler.start()
        log.info("brain scheduler started with %d jobs + task_executor", len(JOB_SCHEDULE))
        for job in JOB_SCHEDULE:
            log.info("  [%s] next=%s", job.name, job.next_run_str(self._scheduler))

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=True)
        self._tick_pool.shutdown(wait=False, cancel_futures=True)

    def schedule_inprocess(
        self,
        func: Callable[[], None],
        name: str,
        seconds: int,
        description: str = "",
    ) -> None:
        """Register an in-process callable on a fixed interval.

        Bypasses the subprocess dispatcher so callers can observe or mutate
        in-process state (e.g. metrics_buf snapshot persistence). The callable
        runs on the FastAPI event loop thread via APScheduler's job executor;
        keep it fast and non-blocking.
        """
        self._scheduler.add_job(
            func,
            trigger=IntervalTrigger(seconds=seconds),
            id=name,
            name=description or name,
            replace_existing=True,
            misfire_grace_time=min(seconds, 60),
            coalesce=True,
        )

    def _tick_executor(self) -> None:
        """Scheduler-side tick. Always returns in <1 ms.

        Submits the actual work to self._tick_pool so a stalled escalation
        (cli_dispatch chain can hit 90s) can never block the scheduler. If
        the previous submission is still in flight, we skip this fire with
        a debug log rather than queuing — we'd rather drop a tick than let
        escalations pile up behind a stuck LLM call.
        """
        prev = self._tick_future
        if prev is not None and not prev.done():
            log.debug("task_executor: previous tick still running, skipping this fire")
            return
        self._tick_future = self._tick_pool.submit(self._tick_work)

    def _tick_work(self) -> None:
        """In-process task executor tick body. Runs on self._tick_pool.

        Two phases:
        1. process_pending — auto-approve tasks above confidence threshold
        2. process_ready — dispatch approved tasks to OpenClaw agents
        """
        import sys as _sys

        _sys.path.insert(0, str(Path(__file__).resolve().parent))
        try:
            from autopilot import is_enabled

            if not is_enabled():
                return
            from task_queue import task_queue

            task_queue.process_pending()  # returns (approved, escalated) — escalation self-dispatches
            task_queue.process_ready()
        except Exception as e:
            log.warning("task_executor tick failed: %s", e)

    _MAX_PENDING_AGE_S = 3600  # reap entries older than 1h even if PID still alive

    def _reap_completions(self) -> None:
        """Drain _pending_completions for any subprocess that has exited.

        Checks each tracked PID with kill(0). Three outcomes per entry:
          - process still alive, age < MAX → keep pending
          - process gone → record_completion + drop from pending + running
          - process stuck beyond MAX age → record_completion with timeout
            error + drop (prevents unbounded dict growth on stuck jobs)
        """
        if not self._pending_completions:
            return
        now = time.time()
        to_drop: list[str] = []
        for job_name, (start_ts, row_id) in list(self._pending_completions.items()):
            pid = self._running_jobs.get(job_name)
            if not pid or pid <= 0:
                self.record_completion(job_name, row_id, start_ts)
                to_drop.append(job_name)
                continue
            try:
                os.kill(pid, 0)
                alive = True
            except (ProcessLookupError, PermissionError):
                alive = False
            age = now - start_ts
            if not alive:
                self.record_completion(job_name, row_id, start_ts)
                to_drop.append(job_name)
            elif age > self._MAX_PENDING_AGE_S:
                self.record_completion(
                    job_name,
                    row_id,
                    start_ts,
                    error=f"reaper_timeout_{int(age)}s",
                )
                to_drop.append(job_name)
        for name in to_drop:
            self._pending_completions.pop(name, None)
            self._running_jobs.pop(name, None)

    def _pid_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def _cleanup_dead_running_locked(self) -> None:
        for name, pid in list(self._running_jobs.items()):
            if not self._pid_alive(pid):
                self._running_jobs.pop(name, None)

    def _resource_blocker_locked(self, job_name: str) -> str | None:
        spec = JOB_BY_NAME.get(job_name)
        if spec is None:
            return None
        self._cleanup_dead_running_locked()
        running_specs = [
            JOB_BY_NAME[name]
            for name in self._running_jobs
            if name != job_name and name in JOB_BY_NAME
        ]
        if spec.resource_class == "heavy":
            limit = self._resource_limits.get("heavy", 0)
            running = sum(1 for item in running_specs if item.resource_class == "heavy")
            if limit and running >= limit:
                return f"heavy:{running}/{limit}"
        for tag in ("llm", "embedder", "index"):
            if tag not in spec.resource_tags:
                continue
            limit = self._resource_limits.get(tag, 0)
            running = sum(1 for item in running_specs if tag in item.resource_tags)
            if limit and running >= limit:
                return f"{tag}:{running}/{limit}"
        return None

    def _resource_usage_locked(self) -> dict[str, dict]:
        self._cleanup_dead_running_locked()
        usage = {
            key: {"limit": limit, "running": 0, "running_jobs": []}
            for key, limit in self._resource_limits.items()
        }
        for job_name in self._running_jobs:
            spec = JOB_BY_NAME.get(job_name)
            if spec is None:
                continue
            if spec.resource_class == "heavy" and "heavy" in usage:
                usage["heavy"]["running"] += 1
                usage["heavy"]["running_jobs"].append(job_name)
            for tag in ("llm", "embedder", "index"):
                if tag in spec.resource_tags and tag in usage:
                    usage[tag]["running"] += 1
                    usage[tag]["running_jobs"].append(job_name)
        return usage

    def _defer_job(self, job_name: str, reason: str) -> None:
        now = datetime.now(UTC)
        retry_at = now + timedelta(seconds=self._resource_defer_s)
        self._scheduler.add_job(
            self._fire,
            trigger=DateTrigger(run_date=retry_at),
            id=f"resource_retry:{job_name}",
            args=[job_name],
            name=f"Resource retry for {job_name}",
            replace_existing=True,
            misfire_grace_time=max(30, min(self._resource_defer_s, 300)),
            coalesce=True,
        )
        previous = self._resource_defers.get(job_name, {})
        self._resource_defers[job_name] = {
            "reason": reason,
            "deferred_at": now.isoformat(),
            "retry_at": retry_at.isoformat(),
            "defer_seconds": self._resource_defer_s,
            "count": int(previous.get("count") or 0) + 1,
        }
        log.info(
            "scheduler: defer %s for %ss due to resource budget %s",
            job_name,
            self._resource_defer_s,
            reason,
        )

    _ALERT_THRESHOLD = 3  # consecutive failures before alerting

    def _fire(self, job_name: str) -> None:
        """APScheduler callback — dispatch the job and record to history."""
        # Skip if a prior run (scheduled or manual) is still alive. Prevents
        # two concurrent drains racing on the same `status='pending'` rows
        # with no SKIP LOCKED semantics → duplicate LLM calls / side effects.
        # 2026-04-18: hold _state_lock across the check-then-reserve so _fire
        # and trigger_now can't both slip past the guard simultaneously.
        # Hold lock across check + dispatch + reservation so trigger_now
        # cannot slip between the "not running" decision and the pid store.
        # Dispatcher is a subprocess.Popen fire-and-forget (fast), so the
        # critical section is microseconds, not seconds.
        with self._state_lock:
            if job_name in self._running_jobs:
                old_pid = self._running_jobs[job_name]
                if self._pid_alive(old_pid):
                    log.info("scheduler: skip %s — already running (pid=%d)", job_name, old_pid)
                    return
                self._running_jobs.pop(job_name, None)  # stale, fall through

            blocker = self._resource_blocker_locked(job_name)
            if blocker:
                self._defer_job(job_name, blocker)
                return

            start_ts = time.time()
            started = datetime.now().isoformat(timespec="seconds")
            pid = -1
            error = None
            try:
                if self._dispatcher is None:
                    raise RuntimeError("dispatcher not registered")
                pid = self._dispatcher(job_name)
                if pid > 0:
                    self._running_jobs[job_name] = pid
                    self._resource_defers.pop(job_name, None)
            except Exception as e:
                error = str(e)[:200]
                log.warning("job %s dispatch failed: %s", job_name, error)

        entry = {
            "started_at": started,
            "pid": pid,
            "error": error,
            "finished_at": None,
            "duration_ms": None,
        }
        history = self._history.setdefault(job_name, [])
        history.append(entry)
        if len(history) > self._MAX_HISTORY:
            history.pop(0)
        row_id = self._persist_entry(job_name, entry)

        if error:
            # Dispatch failed — mark completed immediately
            self.record_completion(job_name, row_id, start_ts, error)
        elif pid > 0:
            self._pending_completions[job_name] = (start_ts, row_id)

        # Alert on consecutive failures
        if error:
            recent_errors = sum(1 for h in history[-self._ALERT_THRESHOLD :] if h.get("error"))
            if recent_errors >= self._ALERT_THRESHOLD and job_name not in self._alerted_jobs:
                self._alerted_jobs.add(job_name)
                self._alert_failure(job_name, error)
        else:
            self._alerted_jobs.discard(job_name)  # reset on success

    def _alert_failure(self, job_name: str, last_error: str) -> None:
        """Send Telegram alert to Chris when a job fails 3+ times consecutively.

        2026-04-17 fix: was dispatching via cli_llm which ignores agent=jenna
        and returns a codex text response — never actually reached Telegram.
        Now uses unified telegram_alert module (direct Bot API, no LLM)."""
        try:
            from telegram_alert import send_chris_telegram

            msg = (
                f"[BRAIN ALERT] Job '{job_name}' failed {self._ALERT_THRESHOLD}x consecutively.\n"
                f"Last error: {(last_error or '')[:300]}"
            )
            send_chris_telegram(msg, source=f"scheduler:{job_name}", severity="warn")
        except Exception as exc:
            log.error("failed to send job failure alert for %s: %s", job_name, exc)

    def list_jobs(self) -> list[dict]:
        jobs = []
        with self._state_lock:
            running_jobs = dict(self._running_jobs)
            defers = dict(self._resource_defers)
        for spec in JOB_SCHEDULE:
            aps_job = self._scheduler.get_job(spec.name) if self._scheduler.running else None
            next_run = aps_job.next_run_time.isoformat() if aps_job and aps_job.next_run_time else None
            history = self._history.get(spec.name, [])
            last = history[-1] if history else None
            jobs.append(
                {
                    "name": spec.name,
                    "description": spec.description,
                    "agent": spec.agent,
                    "resource_class": spec.resource_class,
                    "resource_tags": list(spec.resource_tags),
                    "next_run": next_run,
                    "running_pid": running_jobs.get(spec.name),
                    "resource_defer": defers.get(spec.name),
                    "last_run": last,
                    "run_count": len(history),
                }
            )
        return jobs

    def resource_status(self) -> dict:
        with self._state_lock:
            usage = self._resource_usage_locked()
            defers = dict(self._resource_defers)
            running = dict(self._running_jobs)

        pending_retries = {}
        for job_name, meta in defers.items():
            aps_job = self._scheduler.get_job(f"resource_retry:{job_name}") if self._scheduler.running else None
            next_run = aps_job.next_run_time.isoformat() if aps_job and aps_job.next_run_time else meta.get("retry_at")
            pending_retries[job_name] = {**meta, "next_run": next_run}

        return {
            "limits": dict(self._resource_limits),
            "usage": usage,
            "defer_seconds": self._resource_defer_s,
            "running_jobs": running,
            "pending_retries": pending_retries,
        }

    def get_history(self, job_name: str) -> list[dict]:
        return list(self._history.get(job_name, []))

    def trigger_now(self, job_name: str) -> int:
        """Run a job immediately (manual trigger). Returns pid."""
        if self._dispatcher is None:
            raise RuntimeError("scheduler not started")
        # 2026-04-18: lock the check-then-reserve block so _fire and
        # trigger_now can't both dispatch on a stale "not running" check.
        with self._state_lock:
            if job_name in self._running_jobs:
                old_pid = self._running_jobs[job_name]
                if self._pid_alive(old_pid):
                    raise ValueError(f"{job_name} already running (pid={old_pid})")
                del self._running_jobs[job_name]  # stale entry, clean up
            blocker = self._resource_blocker_locked(job_name)
            if blocker:
                raise ValueError(f"{job_name} blocked by resource budget ({blocker})")
            start_ts = time.time()
            pid = self._dispatcher(job_name)
            if pid > 0:
                self._running_jobs[job_name] = pid
                self._resource_defers.pop(job_name, None)
        entry = {
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "pid": pid,
            "error": None,
            "manual": True,
            "finished_at": None,
            "duration_ms": None,
        }
        history = self._history.setdefault(job_name, [])
        history.append(entry)
        if len(history) > self._MAX_HISTORY:
            history.pop(0)
        row_id = self._persist_entry(job_name, entry)
        if pid > 0:
            self._pending_completions[job_name] = (start_ts, row_id)
        return pid


# Module-level singleton (server.py imports this)
brain_scheduler = BrainScheduler()
