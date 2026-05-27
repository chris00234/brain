"""brain_core/task_queue.py — SQLite-backed task store with state machine.

Provides a durable task queue for the brain's autonomous execution layer.
Tasks flow through a state machine: pending -> approved -> assigned -> running
-> completed/failed. Supports dependency tracking, goal grouping, and
outcome recording for accuracy calibration.

Database: BRAIN_LOGS_DIR / "autonomy.db" (WAL mode, thread-safe via
thread-local connections — same pattern as embed_cache.py).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger("brain.task_queue")

# Capped thread pool for fire-and-forget background work (heuristic/procedure extraction).
# Prevents unbounded thread spawning under burst load.
_bg_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tq_bg")



def _record_failure_lesson_bg(
    task_description: str,
    failure_reason: str,
    agent_id: str,
    context: str = "",
    *,
    db_path: Path | str | None = None,
    attempt_id: str = "",
    task_id: str = "",
) -> None:
    """Background Reflexion lesson write for failed/deferred task execution."""
    lesson_id = ""
    status = "record_failed"
    error = ""
    try:
        import failure_memory

        lesson_id = failure_memory.record_failure_lesson(
            task_description=task_description,
            failure_reason=failure_reason,
            agent_id=agent_id or "system",
            context=context,
        ) or ""
        status = "recorded" if lesson_id else "record_failed"
    except Exception as exc:
        error = str(exc)[:200]
        log.debug("failure lesson bg failed: %s", exc)
    if status != "recorded":
        # LLM-based recorder failed (e.g. harness unreachable, gateway down,
        # codex unregistered). Fall back to the deterministic infra path so
        # the SLO never breaches on a failure that produced no lesson.
        try:
            import failure_memory

            lesson_id = failure_memory.record_infra_failure_lesson(
                task_description=task_description,
                failure_reason=failure_reason,
                agent_id=agent_id or "system",
            ) or ""
            if lesson_id:
                status = "recorded"
                error = ""
        except Exception as exc:
            log.debug("infra-lesson fallback failed: %s", exc)
    if db_path and attempt_id:
        _merge_dispatch_attempt_metadata_db(
            db_path,
            attempt_id,
            {
                "failure_lesson_status": status,
                "failure_lesson_id": lesson_id,
                "failure_lesson_completed_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "failure_lesson_error": error,
            },
        )
    if db_path and task_id:
        _merge_task_metadata_db(
            db_path,
            task_id,
            {
                "last_failure_lesson_status": status,
                "last_failure_lesson_id": lesson_id,
                "last_failure_lesson_completed_at": datetime.now(UTC).isoformat(timespec="seconds"),
            },
        )


def _merge_json_metadata(raw: str | None, patch: dict) -> str:
    try:
        meta = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    meta.update(patch)
    return json.dumps(meta)


def _merge_dispatch_attempt_metadata_db(db_path: Path | str, attempt_id: str, patch: dict) -> None:
    """Best-effort metadata patch for background dispatch-attempt updates."""
    try:
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute(
                "SELECT metadata FROM task_dispatch_attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                return
            conn.execute(
                "UPDATE task_dispatch_attempts SET metadata = ? WHERE id = ?",
                (_merge_json_metadata(row[0], patch), attempt_id),
            )
    except Exception as exc:
        log.debug("dispatch attempt metadata merge failed for %s: %s", attempt_id, exc)


def _merge_task_metadata_db(db_path: Path | str, task_id: str, patch: dict) -> None:
    """Best-effort metadata patch for background task updates."""
    try:
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute("SELECT metadata FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                return
            conn.execute(
                "UPDATE tasks SET metadata = ?, updated_at = ? WHERE id = ?",
                (
                    _merge_json_metadata(row[0], patch),
                    datetime.now(UTC).isoformat(timespec="seconds"),
                    task_id,
                ),
            )
    except Exception as exc:
        log.debug("task metadata merge failed for %s: %s", task_id, exc)

def _materialize_proc_bg(proc: dict) -> None:
    """Background pool task: materialize procedure as SKILL.md files."""
    try:
        from skill_materializer import materialize

        materialize(proc)
    except Exception as exc:
        log.debug("skill materialize bg failed: %s", exc)


# ── Valid state transitions ──────────────────────────────────
TRANSITIONS = {
    "approve": ({"pending"}, "approved"),
    "assign": ({"approved"}, "assigned"),
    "start": ({"approved", "assigned", "resumed"}, "running"),
    "complete": ({"running"}, "completed"),
    "fail": ({"pending", "approved", "assigned", "running"}, "failed"),
    "defer": ({"running"}, "approved"),
    "pause": ({"running"}, "paused"),
    "resume": ({"paused"}, "resumed"),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    priority INTEGER DEFAULT 5,
    assigned_agent TEXT,
    parent_goal_id TEXT,
    depends_on TEXT DEFAULT '[]',
    confidence REAL DEFAULT 0.0,
    confidence_reasoning TEXT DEFAULT '',
    created_by TEXT DEFAULT 'brain',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    result TEXT,
    error TEXT,
    execution_log TEXT DEFAULT '[]',
    metadata TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS goals (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_by TEXT DEFAULT 'chris',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    metadata TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS outcomes (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    domain TEXT DEFAULT 'general',
    brain_recommendation TEXT,
    actual_action TEXT,
    chris_override INTEGER DEFAULT 0,
    override_reason TEXT DEFAULT '',
    confidence_was REAL,
    procedure_ids TEXT DEFAULT '[]',
    lesson_ids TEXT DEFAULT '[]',
    created_at TEXT NOT NULL,
    acked INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS task_dispatch_attempts (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    agent TEXT DEFAULT '',
    backend TEXT DEFAULT '',
    model TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'started',
    error_class TEXT DEFAULT '',
    error TEXT DEFAULT '',
    result_preview TEXT DEFAULT '',
    prompt_chars INTEGER DEFAULT 0,
    response_chars INTEGER DEFAULT 0,
    duration_ms INTEGER DEFAULT 0,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    metadata TEXT DEFAULT '{}',
    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS accuracy_tracker (
    domain TEXT PRIMARY KEY,
    total_recommendations INTEGER DEFAULT 0,
    correct_recommendations INTEGER DEFAULT 0,
    override_count INTEGER DEFAULT 0,
    last_updated TEXT
);

CREATE TABLE IF NOT EXISTS procedures (
    id TEXT PRIMARY KEY,
    task_type TEXT NOT NULL,
    title TEXT NOT NULL,
    steps TEXT NOT NULL,
    preconditions TEXT DEFAULT '',
    tools TEXT DEFAULT '[]',
    success_count INTEGER DEFAULT 1,
    last_used TEXT,
    created_at TEXT NOT NULL,
    source TEXT DEFAULT 'extraction'
);
"""


class TaskQueue:
    def __init__(self, db_path: Path | str | None = None):
        if db_path is None:
            try:
                from config import BRAIN_LOGS_DIR
            except ImportError:
                BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")
            db_path = BRAIN_LOGS_DIR / "autonomy.db"
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        # Run migrations on init thread
        self._migrate()

    # ── Connection management ────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA cache_size=-8000")
            try:
                from db_maintenance import apply_hot_db_pragmas as _apply_hot_db_pragmas

                _apply_hot_db_pragmas(conn)
            except Exception:
                # Long-lived autonomy.db connection — losing the limit only
                # means the daily TRUNCATE remains the sole ceiling.
                pass
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def _migrate(self) -> None:
        conn = self._conn()
        conn.executescript(_SCHEMA)
        conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_tasks_status_priority ON tasks(status, priority, created_at);
            CREATE INDEX IF NOT EXISTS idx_tasks_parent_goal ON tasks(parent_goal_id);
            CREATE INDEX IF NOT EXISTS idx_outcomes_domain ON outcomes(domain);
            CREATE INDEX IF NOT EXISTS idx_outcomes_task_id ON outcomes(task_id);
            CREATE INDEX IF NOT EXISTS idx_outcomes_created_at ON outcomes(created_at);
            CREATE INDEX IF NOT EXISTS idx_dispatch_attempts_task_id ON task_dispatch_attempts(task_id);
            CREATE INDEX IF NOT EXISTS idx_dispatch_attempts_trace_id ON task_dispatch_attempts(trace_id);
            CREATE INDEX IF NOT EXISTS idx_dispatch_attempts_status ON task_dispatch_attempts(status);
            CREATE INDEX IF NOT EXISTS idx_procedures_task_type ON procedures(task_type);
        """)
        # v3 plan: brain_loop goal extensions. ALTER TABLE ADD COLUMN is idempotent
        # via try/except — SQLite rejects duplicates with OperationalError, which
        # we ignore so re-running migrate on subsequent starts is a no-op.
        goal_extensions = [
            ("next_check_at", "TEXT"),
            ("owner_agent", "TEXT DEFAULT 'chris'"),
            ("brain_notes", "TEXT DEFAULT ''"),
            ("interventions", "TEXT DEFAULT '[]'"),
        ]
        for col, col_type in goal_extensions:
            try:
                conn.execute(f"ALTER TABLE goals ADD COLUMN {col} {col_type}")
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e).lower():
                    log.warning("goals ALTER ADD %s failed: %s", col, e)
        outcome_extensions = [
            ("procedure_ids", "TEXT DEFAULT '[]'"),
            ("lesson_ids", "TEXT DEFAULT '[]'"),
        ]
        for col, col_type in outcome_extensions:
            try:
                conn.execute(f"ALTER TABLE outcomes ADD COLUMN {col} {col_type}")
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e).lower():
                    log.warning("outcomes ALTER ADD %s failed: %s", col, e)
        conn.commit()

    # ── Helpers ──────────────────────────────────────────────

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds")

    @staticmethod
    def _gen_id(prefix: str = "task") -> str:
        return f"{prefix}_{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        for key in ("depends_on", "execution_log", "metadata"):
            if key in d and isinstance(d[key], str):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d

    def _merge_task_metadata(self, task_id: str, patch: dict) -> None:
        """Best-effort merge into tasks.metadata without changing task status."""

        try:
            conn = self._conn()
            row = conn.execute("SELECT metadata FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                return
            try:
                meta = json.loads(row["metadata"] or "{}")
            except (json.JSONDecodeError, TypeError):
                meta = {}
            if not isinstance(meta, dict):
                meta = {}
            meta.update(patch)
            conn.execute(
                "UPDATE tasks SET metadata = ?, updated_at = ? WHERE id = ?",
                (json.dumps(meta), self._now(), task_id),
            )
            conn.commit()
        except Exception as exc:
            log.debug("task metadata merge failed for %s: %s", task_id, exc)

    @staticmethod
    def _dispatch_row_to_dict(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        if isinstance(d.get("metadata"), str):
            try:
                d["metadata"] = json.loads(d["metadata"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d

    # ── Task CRUD ────────────────────────────────────────────

    def create_task(
        self,
        title: str,
        description: str = "",
        assigned_agent: str | None = None,
        priority: int = 5,
        parent_goal_id: str | None = None,
        confidence: float = 0.0,
        confidence_reasoning: str = "",
        created_by: str = "brain",
        depends_on: list[str] | None = None,
        metadata: dict | None = None,
    ) -> dict:
        now = self._now()
        task_id = self._gen_id("task")
        task_metadata = dict(metadata or {})
        task_metadata.setdefault(
            "trace_id",
            task_metadata.get("source_message_id") or task_metadata.get("trace_id") or task_id,
        )
        conn = self._conn()
        conn.execute(
            """INSERT INTO tasks
               (id, title, description, status, priority, assigned_agent,
                parent_goal_id, depends_on, confidence, confidence_reasoning,
                created_by, created_at, updated_at, execution_log, metadata)
               VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       '[]', ?)""",
            (
                task_id,
                title,
                description,
                priority,
                assigned_agent,
                parent_goal_id,
                json.dumps(depends_on or []),
                confidence,
                confidence_reasoning,
                created_by,
                now,
                now,
                json.dumps(task_metadata),
            ),
        )
        conn.commit()
        log.info("created task %s: %s", task_id, title)
        return self.get_task(task_id)  # type: ignore[return-value]

    def get_task(self, task_id: str) -> dict | None:
        conn = self._conn()
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._row_to_dict(row)

    def _trace_id_for_task(self, task: dict | None, task_id: str) -> str:
        meta = task.get("metadata") if task else {}
        if not isinstance(meta, dict):
            meta = {}
        return str(meta.get("trace_id") or meta.get("source_message_id") or task_id)

    def list_tasks(
        self,
        status: str | None = None,
        agent: str | None = None,
        parent_goal_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if agent:
            clauses.append("assigned_agent = ?")
            params.append(agent)
        if parent_goal_id:
            clauses.append("parent_goal_id = ?")
            params.append(parent_goal_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.extend([limit, offset])
        conn = self._conn()
        rows = conn.execute(
            f"SELECT * FROM tasks{where} ORDER BY priority ASC, created_at ASC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ── State transitions ────────────────────────────────────

    _ALLOWED_EXTRA_COLS = {"assigned_agent", "result", "error"}

    def _transition(
        self,
        task_id: str,
        from_statuses: set[str],
        to_status: str,
        by: str = "system",
        **extra,
    ) -> dict:
        # Validate extra columns before touching DB
        for key in extra:
            if key not in self._ALLOWED_EXTRA_COLS:
                raise ValueError(f"illegal column in transition: {key}")

        conn = self._conn()
        now = self._now()

        # Read current state to build execution_log entry
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise ValueError(f"task {task_id} not found")
        task = self._row_to_dict(row)
        current = task["status"]
        exec_log = task.get("execution_log", [])
        if not isinstance(exec_log, list):
            exec_log = []
        exec_log.append({"from": current, "to": to_status, "at": now, "by": by})

        updates = {"status": to_status, "updated_at": now, "execution_log": json.dumps(exec_log)}
        if to_status == "running":
            updates["started_at"] = now
        if current == "running" and to_status == "approved":
            updates["started_at"] = None
            updates["completed_at"] = None
        if to_status in ("completed", "failed"):
            updates["completed_at"] = now
        updates.update(extra)

        # Atomic check-and-update: WHERE guards the from_statuses
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        status_placeholders = ", ".join("?" for _ in from_statuses)
        vals = list(updates.values()) + [task_id] + list(from_statuses)
        cursor = conn.execute(
            f"UPDATE tasks SET {set_clause} WHERE id = ? AND status IN ({status_placeholders})",
            vals,
        )
        conn.commit()

        if cursor.rowcount == 0:
            # Re-read to get actual current status for error message
            fresh = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            actual = fresh["status"] if fresh else "unknown"
            raise ValueError(
                f"cannot transition {task_id} from '{actual}' to '{to_status}' "
                f"(allowed from: {from_statuses})"
            )

        log.info("task %s: %s -> %s (by %s)", task_id, current, to_status, by)
        return self.get_task(task_id)  # type: ignore[return-value]

    def approve_task(self, task_id: str, by: str = "chris") -> dict:
        return self._transition(task_id, {"pending"}, "approved", by=by)

    def assign_task(self, task_id: str, agent: str, by: str = "system") -> dict:
        return self._transition(task_id, {"approved"}, "assigned", by=by, assigned_agent=agent)

    def start_task(self, task_id: str, by: str = "system") -> dict:
        return self._transition(task_id, {"approved", "assigned", "resumed"}, "running", by=by)

    def complete_task(self, task_id: str, result: str = "", by: str = "system") -> dict:
        updated = self._transition(task_id, {"running"}, "completed", by=by, result=result, error="")
        # Auto-complete parent goal when all subtasks are done
        goal_id = updated.get("parent_goal_id")
        if goal_id:
            self._maybe_complete_goal(goal_id, by=by)
        return updated

    def auto_complete_task(self, task_id: str, *, result: str = "", by: str = "system") -> dict:
        """Skip the running→completed gate for system-driven evaluators.

        Used by subtask_evaluator when a measurable metric clears its target
        without anyone ever explicitly transitioning the subtask through
        approved/running. Functionally the same as `complete_task`, but
        allows any non-terminal source state.
        """
        updated = self._transition(
            task_id,
            {"pending", "approved", "assigned", "running", "resumed", "paused"},
            "completed",
            by=by,
            result=result,
            error="",
        )
        goal_id = updated.get("parent_goal_id")
        if goal_id:
            self._maybe_complete_goal(goal_id, by=by)
        return updated

    def _maybe_complete_goal(self, goal_id: str, by: str = "system") -> None:
        """Complete goal if all child tasks are in terminal state (completed/failed)."""
        progress = self.get_goal_progress(goal_id)
        if progress["total"] == 0:
            return
        # Count all non-terminal states (pending, running, approved, assigned, paused, resumed)
        terminal = progress.get("completed", 0) + progress.get("failed", 0)
        non_terminal = progress["total"] - terminal
        if non_terminal == 0 and progress["completed"] > 0:
            try:
                self.complete_goal(goal_id, by=by)
                log.info("auto-completed goal %s (all %d subtasks done)", goal_id, progress["total"])
            except ValueError:
                pass  # already completed or cancelled

    def fail_task(self, task_id: str, error: str = "", by: str = "system") -> dict:
        return self._transition(
            task_id, {"pending", "approved", "assigned", "running"}, "failed", by=by, error=error
        )

    def defer_task(self, task_id: str, error: str = "", retry_after_s: int = 300, by: str = "system") -> dict:
        """Move a running task back to approved after a transient dispatch outage.

        Circuit-breaker and provider-cooldown fast-fails are infrastructure
        throttles, not task failures. Keep the task ready for a later retry and
        record the retry window in metadata so the executor does not hammer a
        known-down upstream every 30 seconds.
        """
        retry_after_s = max(30, min(int(retry_after_s or 300), 14_400))
        next_attempt_at = (datetime.now(UTC) + timedelta(seconds=retry_after_s)).isoformat(
            timespec="seconds"
        )
        self._merge_task_metadata(
            task_id,
            {
                "last_dispatch_error": error[:300],
                "dispatch_retry_after_s": retry_after_s,
                "next_attempt_at": next_attempt_at,
            },
        )
        return self._transition(task_id, {"running"}, "approved", by=by, error=error[:500])

    def pause_task(self, task_id: str, by: str = "system") -> dict:
        return self._transition(task_id, {"running"}, "paused", by=by)

    def resume_task(self, task_id: str, by: str = "system") -> dict:
        return self._transition(task_id, {"paused"}, "resumed", by=by)

    # ── Dispatch attempt audit ───────────────────────────────

    def record_dispatch_attempt_start(
        self,
        task_id: str,
        *,
        agent: str = "",
        backend: str = "",
        model: str = "",
        prompt_chars: int = 0,
        metadata: dict | None = None,
    ) -> dict:
        """Create an immutable dispatch-attempt row before calling the backend."""

        task = self.get_task(task_id)
        if not task:
            raise ValueError(f"task {task_id} not found")
        trace_id = self._trace_id_for_task(task, task_id)
        conn = self._conn()
        row = conn.execute(
            "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM task_dispatch_attempts WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        attempt_no = int(row[0] or 1) if row else 1
        attempt_id = self._gen_id("dispatch")
        now = self._now()
        conn.execute(
            """INSERT INTO task_dispatch_attempts
               (id, task_id, trace_id, attempt_no, agent, backend, model,
                status, prompt_chars, started_at, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'started', ?, ?, ?)""",
            (
                attempt_id,
                task_id,
                trace_id,
                attempt_no,
                agent,
                backend,
                model,
                int(prompt_chars or 0),
                now,
                json.dumps(metadata or {}),
            ),
        )
        conn.commit()
        return self.get_dispatch_attempt(attempt_id)  # type: ignore[return-value]

    def finish_dispatch_attempt(
        self,
        attempt_id: str,
        *,
        status: str,
        backend: str = "",
        model: str = "",
        error_class: str = "",
        error: str = "",
        result_preview: str = "",
        response_chars: int = 0,
        duration_ms: int = 0,
        metadata: dict | None = None,
    ) -> dict | None:
        """Close a dispatch-attempt row with backend result evidence."""

        if status not in {"completed", "failed", "deferred"}:
            raise ValueError(f"invalid dispatch attempt status: {status}")
        conn = self._conn()
        now = self._now()
        existing = conn.execute(
            "SELECT metadata FROM task_dispatch_attempts WHERE id = ?",
            (attempt_id,),
        ).fetchone()
        merged_metadata = _merge_json_metadata(
            existing["metadata"] if existing is not None else "{}",
            metadata or {},
        )
        conn.execute(
            """UPDATE task_dispatch_attempts
               SET status = ?, backend = COALESCE(NULLIF(?, ''), backend),
                   model = COALESCE(NULLIF(?, ''), model),
                   error_class = ?, error = ?, result_preview = ?,
                   response_chars = ?, duration_ms = ?, completed_at = ?,
                   metadata = ?
               WHERE id = ?""",
            (
                status,
                backend,
                model,
                error_class[:80],
                error[:500],
                result_preview[:500],
                int(response_chars or 0),
                int(duration_ms or 0),
                now,
                merged_metadata,
                attempt_id,
            ),
        )
        conn.commit()
        return self.get_dispatch_attempt(attempt_id)

    def get_dispatch_attempt(self, attempt_id: str) -> dict | None:
        conn = self._conn()
        row = conn.execute("SELECT * FROM task_dispatch_attempts WHERE id = ?", (attempt_id,)).fetchone()
        return self._dispatch_row_to_dict(row)

    def list_dispatch_attempts(
        self,
        *,
        task_id: str | None = None,
        trace_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list = []
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        if trace_id:
            clauses.append("trace_id = ?")
            params.append(trace_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.extend([limit, offset])
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM task_dispatch_attempts"
            f"{where} ORDER BY started_at DESC, attempt_no DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [self._dispatch_row_to_dict(r) for r in rows]

    def get_task_execution_truth(self, task_id: str) -> dict:
        """Return one auditable chain: task -> dispatch attempts -> outcomes."""

        task = self.get_task(task_id)
        if not task:
            raise ValueError(f"task {task_id} not found")
        attempts = self.list_dispatch_attempts(task_id=task_id, limit=100)
        outcomes = [
            row
            for row in self.list_outcomes(limit=100)
            if row.get("task_id") == task_id
        ]
        terminal = task.get("status") in {"completed", "failed"}
        has_closed_attempt = any(a.get("status") in {"completed", "failed", "deferred"} for a in attempts)
        return {
            "task": task,
            "trace_id": self._trace_id_for_task(task, task_id),
            "dispatch_attempts": attempts,
            "outcomes": outcomes,
            "evidence": {
                "has_dispatch_attempt": bool(attempts),
                "has_closed_dispatch_attempt": has_closed_attempt,
                "has_outcome": bool(outcomes),
                "terminal_has_outcome": (not terminal) or bool(outcomes),
            },
        }

    # ── Queries ──────────────────────────────────────────────

    def get_ready_tasks(self) -> list[dict]:
        """Return approved/assigned tasks whose dependencies are all completed.

        Uses a single query: fetches candidate tasks, then batch-checks all
        dependency IDs in one query instead of N+1 per-task lookups.
        """
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status IN ('approved', 'assigned') "
            "ORDER BY priority ASC, created_at ASC"
        ).fetchall()
        candidates = [self._row_to_dict(row) for row in rows]
        if not candidates:
            return []

        # Collect all unique dependency IDs across all candidates
        all_dep_ids: set[str] = set()
        for task in candidates:
            deps = task.get("depends_on", [])
            if deps:
                all_dep_ids.update(deps)

        # Single batch query: which of these deps are completed?
        completed_ids: set[str] = set()
        if all_dep_ids:
            dep_list = list(all_dep_ids)
            placeholders = ",".join("?" for _ in dep_list)
            completed_rows = conn.execute(
                f"SELECT id FROM tasks WHERE id IN ({placeholders}) AND status = 'completed'",
                dep_list,
            ).fetchall()
            completed_ids = {r["id"] for r in completed_rows}

        # Filter: tasks with no deps or all deps completed
        ready = []
        for task in candidates:
            deps = task.get("depends_on", [])
            deps_ready = not deps or all(d in completed_ids for d in deps)
            if not deps_ready:
                continue
            meta = task.get("metadata") or {}
            next_attempt_at = meta.get("next_attempt_at")
            if next_attempt_at:
                try:
                    next_dt = datetime.fromisoformat(next_attempt_at)
                    if next_dt.tzinfo is None:
                        next_dt = next_dt.replace(tzinfo=UTC)
                    if next_dt > datetime.now(UTC):
                        continue
                except Exception:
                    pass
            ready.append(task)
        return ready

    def get_goal_progress(self, goal_id: str) -> dict:
        """Return progress summary for a goal's child tasks."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM tasks WHERE parent_goal_id = ? GROUP BY status",
            (goal_id,),
        ).fetchall()
        counts = {r["status"]: r["cnt"] for r in rows}
        total = sum(counts.values())
        completed = counts.get("completed", 0)
        return {
            "total": total,
            "completed": completed,
            "failed": counts.get("failed", 0),
            "pending": counts.get("pending", 0),
            "running": counts.get("running", 0),
            "pct": round(completed / total * 100, 1) if total else 0.0,
        }

    def pause_running_tasks(self, by: str = "system") -> int:
        """Bulk pause all running tasks. Returns count paused."""
        conn = self._conn()
        rows = conn.execute("SELECT id FROM tasks WHERE status = 'running'").fetchall()
        count = 0
        for row in rows:
            try:
                self.pause_task(row["id"], by=by)
                count += 1
            except ValueError:
                pass
        return count

    def requeue_running_orphans(self, by: str = "startup_reconcile") -> int:
        """Requeue tasks left running by a prior brain-server process.

        Task execution happens inside the scheduler process. After a launchd
        restart there is no worker left that can record completion for rows
        already marked running, so leaving them as running creates the same
        false "under the hood" state Chris reported. Startup reconciliation
        moves them back to approved for a fresh dispatch attempt.
        """
        conn = self._conn()
        now = self._now()
        rows = conn.execute("SELECT * FROM tasks WHERE status = 'running'").fetchall()
        count = 0
        for row in rows:
            task = self._row_to_dict(row)
            meta = task.get("metadata") or {}
            meta["orphaned_running_requeued_at"] = now
            meta["orphaned_running_requeued_by"] = by
            if task.get("started_at"):
                meta["orphaned_started_at"] = task["started_at"]
            exec_log = task.get("execution_log", [])
            if not isinstance(exec_log, list):
                exec_log = []
            exec_log.append(
                {
                    "from": "running",
                    "to": "approved",
                    "at": now,
                    "by": by,
                    "reason": "running task orphaned by server restart",
                }
            )
            conn.execute(
                "UPDATE tasks SET status='approved', updated_at=?, started_at=NULL, "
                "completed_at=NULL, error='', execution_log=?, metadata=? WHERE id=?",
                (now, json.dumps(exec_log, ensure_ascii=False), json.dumps(meta), task["id"]),
            )
            count += 1
        conn.commit()
        if count:
            log.warning("requeued %d orphaned running task(s) on startup", count)
        return count

    # ── Autopilot gate ───────────────────────────────────────

    def _get_last_escalated(self, task_id: str) -> str | None:
        """Read last escalation timestamp from task metadata (persists across restarts)."""
        task = self.get_task(task_id)
        if task:
            meta = task.get("metadata") or {}
            return meta.get("last_escalated_at")
        return None

    def _set_last_escalated(self, task_id: str, ts: str) -> None:
        """Persist escalation timestamp into task metadata."""
        conn = self._conn()
        task = self.get_task(task_id)
        if task:
            meta = task.get("metadata") or {}
            meta["last_escalated_at"] = ts
            conn.execute("UPDATE tasks SET metadata = ? WHERE id = ?", (json.dumps(meta), task_id))
            conn.commit()

    def _append_task_execution_event(self, task_id: str, event: dict) -> None:
        """Append a non-transition evidence event to task execution_log."""
        conn = self._conn()
        task = self.get_task(task_id)
        if not task:
            return
        log_rows = task.get("execution_log") or []
        if not isinstance(log_rows, list):
            log_rows = []
        event = {**event, "at": event.get("at") or self._now()}
        log_rows.append(event)
        conn.execute(
            "UPDATE tasks SET execution_log = ?, updated_at = ? WHERE id = ?",
            (json.dumps(log_rows), self._now(), task_id),
        )
        conn.commit()

    def _merge_task_metadata(self, task_id: str, patch: dict) -> None:
        """Merge metadata fields for a task without clobbering existing keys."""
        conn = self._conn()
        task = self.get_task(task_id)
        if not task:
            return
        meta = task.get("metadata") or {}
        meta.update(patch)
        conn.execute(
            "UPDATE tasks SET metadata = ?, updated_at = ? WHERE id = ?",
            (json.dumps(meta), self._now(), task_id),
        )
        conn.commit()

    def process_pending(self) -> tuple[list[dict], list[dict]]:
        """Auto-approve pending tasks above confidence threshold, escalate the rest.

        Returns (approved_tasks, escalated_tasks).
        """
        import sys as _sys
        from pathlib import Path as _Path

        _bc = str(_Path(__file__).parent)
        if _bc not in _sys.path:
            _sys.path.insert(0, _bc)
        import autopilot

        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status = 'pending' ORDER BY priority ASC, created_at ASC LIMIT 20"
        ).fetchall()
        approved = []
        escalation_needed = []
        # Phase 5 autonomy gate
        try:
            from autonomy import authorize as _autonomy_authorize
        except Exception:
            _autonomy_authorize = None  # type: ignore[assignment]

        for row in rows:
            task = self._row_to_dict(row)
            # Phase 5: gate ALL auto-approve via autonomy.authorize("task.approve")
            if _autonomy_authorize is not None:
                gate = _autonomy_authorize("task.approve", context={"task_id": task["id"]})
                if not gate.allowed:
                    log.info(
                        "autonomy gate blocked task.approve for %s: %s",
                        task["id"],
                        gate.reason,
                    )
                    escalation_needed.append(task)
                    continue
                if gate.requires_ack:
                    log.debug(
                        "autonomy L1 — task %s queued for human approval",
                        task["id"],
                    )
                    escalation_needed.append(task)
                    continue
            if autopilot.should_auto_approve(task["confidence"]):
                try:
                    updated = self.approve_task(task["id"], by="autopilot")
                    approved.append(updated)
                    log.info("auto-approved %s (confidence=%.2f)", task["id"], task["confidence"])
                except ValueError as exc:
                    log.warning("auto-approve failed for %s: %s", task["id"], exc)
            else:
                log.debug(
                    "task %s below threshold (confidence=%.2f), needs escalation",
                    task["id"],
                    task["confidence"],
                )
                escalation_needed.append(task)

        if escalation_needed:
            self._escalate_tasks(escalation_needed)

        return approved, escalation_needed

    def _escalate_tasks(self, tasks: list[dict]) -> None:
        """Review below-threshold tasks before notifying Chris.

        Most low-confidence tasks are still handleable by subscription-backed
        LLMs. Chris is notified only for concrete private-knowledge, credential,
        authority, or irreversible-action blockers.
        """
        from datetime import timedelta

        now = datetime.now(UTC)
        cooldown = timedelta(hours=4)

        to_escalate = []
        for t in tasks:
            tid = t["id"]
            last = self._get_last_escalated(tid)
            if last:
                try:
                    last_dt = datetime.fromisoformat(last)
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=UTC)
                    if now - last_dt < cooldown:
                        continue
                except Exception:
                    pass
            to_escalate.append(t)

        if not to_escalate:
            return

        human_required: list[tuple[dict, str]] = []
        llm_review: list[dict] = []
        try:
            from escalation_policy import classify_escalation

            for t in to_escalate:
                route = classify_escalation(
                    title=t.get("title", ""),
                    content=t.get("description", ""),
                    metadata=t.get("metadata") or {},
                )
                if route.notify_human:
                    human_required.append((t, route.reason))
                else:
                    llm_review.append(t)
        except Exception as exc:
            log.debug("escalation policy unavailable, keeping tasks on LLM path: %s", exc)
            llm_review = to_escalate

        handled_ids: set[str] = set()
        if llm_review:
            handled_ids.update(self._review_tasks_with_subscription_llm(llm_review))

        if human_required:
            self._notify_task_evaluation_actions(
                [
                    (
                        t,
                        "held_for_safe_followup",
                        reason,
                        "classifier_human_required",
                    )
                    for t, reason in human_required
                ]
            )
            handled_ids.update(t["id"] for t, _reason in human_required)

        if handled_ids:
            now_iso = self._now()
            for t in to_escalate:
                if t["id"] in handled_ids:
                    self._set_last_escalated(t["id"], now_iso)

    def _review_tasks_with_subscription_llm(self, tasks: list[dict]) -> set[str]:
        """Ask subscription LLM whether tasks are handleable; notify only on blocker."""
        if not tasks:
            return set()

        def _task_review_body(t: dict) -> str:
            return "\n".join(
                [
                    "TASK ESCALATION REVIEW — decide if this can be handled without Chris:",
                    "",
                    f"- [{t['id']}] {t.get('title', '')} "
                    f"(confidence={float(t.get('confidence', 0.0)):.0%}, "
                    f"agent={t.get('assigned_agent') or 'sage'})",
                    f"  description={str(t.get('description', ''))[:700]}",
                ]
            )

        def _approve_for_agent(t: dict, action_text: str) -> bool:
            tid = t["id"]
            task = self.get_task(tid)
            if not task:
                # Unit tests may pass synthetic task dicts directly.
                return True
            routed_at = self._now()
            reason_preview = " ".join(str(action_text or "").split())[:800]
            agent = (task.get("assigned_agent") or "").strip().lower()
            assigned_agent = task.get("assigned_agent") or "sage"
            brain_action = "approved_for_existing_agent"
            if agent in {"", "chris", "human", "user"}:
                assigned_agent = "sage"
                brain_action = "reassigned_to_sage_and_approved"
                conn = self._conn()
                conn.execute(
                    "UPDATE tasks SET assigned_agent = ?, updated_at = ? WHERE id = ?",
                    (assigned_agent, routed_at, tid),
                )
                conn.commit()
            self._merge_task_metadata(
                tid,
                {
                    "task_evaluation_alert_policy": "autonomous_log",
                    "task_evaluation_action": "routed_for_agent_execution",
                    "task_evaluation_brain_action": brain_action,
                    "task_evaluation_decision": "handleable",
                    "task_evaluation_reason": reason_preview,
                    "task_evaluation_source": "llm_handleable",
                    "task_evaluation_routed_at": routed_at,
                    "task_evaluation_next_evidence": f"/brain/tasks/{tid}/execution",
                    "escalation_llm_route": "handleable",
                    "escalation_llm_action": reason_preview,
                    "escalation_llm_routed_at": routed_at,
                },
            )
            self._append_task_execution_event(
                tid,
                {
                    "event": "task_evaluation",
                    "by": "escalation_llm",
                    "decision": "handleable",
                    "action": "routed_for_agent_execution",
                    "brain_action": brain_action,
                    "assigned_agent": assigned_agent,
                    "reason": reason_preview,
                    "evidence": f"/brain/tasks/{tid}/execution",
                    "at": routed_at,
                },
            )
            try:
                self.approve_task(tid, by="escalation_llm")
                log.info("task escalation self-routed %s for agent handling", tid)
                return True
            except ValueError as exc:
                self._merge_task_metadata(
                    tid,
                    {
                        "task_evaluation_brain_action": "evaluation_recorded_approval_skipped",
                        "task_evaluation_approval_error": str(exc)[:200],
                    },
                )
                log.debug("task escalation self-route skipped for %s: %s", tid, exc)
                return False

        try:
            import sys as _sys
            from pathlib import Path as _Path

            _bc = str(_Path(__file__).parent)
            if _bc not in _sys.path:
                _sys.path.insert(0, _bc)
            from cli_llm import dispatch
            from escalation_policy import llm_review_prompt, llm_says_human_needed

            handled: set[str] = set()
            for t in tasks:
                body = _task_review_body(t)
                result = dispatch(
                    agent="sage",
                    message=llm_review_prompt("task_queue", body),
                    thinking="low",
                    timeout=60,
                    backlog_kind="proactive",
                    backlog_payload={"source": "task_queue", "body": body},
                    degraded_placeholder="[Task escalation dispatch failed]",
                )
                if not result.ok:
                    log.warning("task escalation LLM review failed for %s: %s", t.get("id"), result.error)
                    continue
                if llm_says_human_needed(result.text):
                    self._notify_task_evaluation_actions(
                        [
                            (
                                t,
                                "held_for_safe_followup",
                                result.text,
                                "llm_human_needed",
                            )
                        ]
                    )
                    handled.add(t["id"])
                elif _approve_for_agent(t, result.text):
                    handled.add(t["id"])
                log.info("reviewed escalated task %s with subscription LLM", t.get("id"))
            return handled
        except Exception as e:
            log.warning("task escalation LLM review error: %s", e)
        return set()

    def _notify_task_evaluation_actions(
        self,
        actions: list[tuple[dict, str, str, str]],
    ) -> None:
        """Tell Chris what Brain did after task evaluation, without asking for input."""
        if not actions:
            return

        routed_at = self._now()
        lines = ["TASK EVALUATION ACTION — Brain handled these without asking:", ""]
        for t, action, reason, source in actions:
            tid = t["id"]
            reason_preview = " ".join(str(reason or "").split())[:500]
            decision = "human_needed" if source == "llm_human_needed" else "policy_held"
            self._merge_task_metadata(
                tid,
                {
                    "task_evaluation_alert_policy": "action_summary",
                    "task_evaluation_action": action,
                    "task_evaluation_brain_action": action,
                    "task_evaluation_decision": decision,
                    "task_evaluation_reason": reason_preview,
                    "task_evaluation_source": source,
                    "task_evaluation_routed_at": routed_at,
                    "task_evaluation_next_evidence": f"/brain/tasks/{tid}/execution",
                    "escalation_llm_route": decision,
                    "escalation_llm_reason": reason_preview,
                    "escalation_llm_routed_at": routed_at,
                },
            )
            self._append_task_execution_event(
                tid,
                {
                    "event": "task_evaluation",
                    "by": source,
                    "decision": decision,
                    "action": action,
                    "brain_action": action,
                    "reason": reason_preview,
                    "evidence": f"/brain/tasks/{tid}/execution",
                    "at": routed_at,
                },
            )
            lines.append(
                f"- [{tid}] {t.get('title', '')} "
                f"(action={action}, confidence={float(t.get('confidence', 0.0)):.0%})"
            )
            if reason_preview:
                lines.append(f"  reason={reason_preview}")
        self._notify_chris_text(
            "\n".join(lines),
            source="task_queue:evaluation_action_summary",
            severity="info",
        )

    def _notify_chris_text(self, body: str, *, source: str, severity: str = "warn") -> None:
        """Direct Chris notification; avoids LLM path and API spend."""
        try:
            from telegram_alert import send_chris_telegram

            send_chris_telegram(body, source=source, severity=severity)
        except Exception as e:
            log.warning("task evaluation Chris notification failed: %s", e)

    def _record_failure_lesson_async(
        self,
        task: dict,
        failure_reason: str,
        agent_id: str,
        *,
        context: str = "",
        attempt_id: str = "",
    ) -> None:
        """Capture failed/deferred task lessons without blocking dispatch ticks."""
        title = str(task.get("title") or "").strip()
        desc = str(task.get("description") or "").strip()
        task_description = (title + (" — " + desc if desc else ""))[:800]
        if not task_description or not failure_reason:
            return
        if self._is_transient_dispatch_error(failure_reason):
            # Infra failure (backend_cooldown / timeout / rate_limit / gateway).
            # Recording via codex would deadlock on the same throttled backend,
            # so use the deterministic local path. Neo4j MERGE collapses repeats
            # to one lesson row per (agent, error_class).
            try:
                from failure_memory import record_infra_failure_lesson

                lesson_id = record_infra_failure_lesson(
                    task_description=task_description,
                    failure_reason=failure_reason,
                    agent_id=agent_id or "system",
                ) or ""
            except Exception as exc:
                log.debug("infra-lesson record failed: %s", exc)
                lesson_id = ""
            now = self._now()
            status = "recorded" if lesson_id else "record_failed"
            if attempt_id:
                self._merge_dispatch_attempt_metadata(
                    attempt_id,
                    {
                        "failure_lesson_status": status,
                        "failure_lesson_id": lesson_id,
                        "failure_lesson_completed_at": now,
                        "failure_lesson_kind": "infra",
                    },
                )
            task_id = str(task.get("id") or "")
            if task_id:
                self._merge_task_metadata(
                    task_id,
                    {
                        "last_failure_lesson_status": status,
                        "last_failure_lesson_id": lesson_id,
                        "last_failure_lesson_completed_at": now,
                    },
                )
            return
        if attempt_id:
            self._merge_dispatch_attempt_metadata(
                attempt_id,
                {
                    "failure_lesson_status": "submitted",
                    "failure_lesson_submitted_at": self._now(),
                    "failure_lesson_agent": agent_id or "system",
                },
            )
        task_id = str(task.get("id") or "")
        if task_id:
            self._merge_task_metadata(
                task_id,
                {
                    "last_failure_lesson_status": "submitted",
                    "last_failure_lesson_submitted_at": self._now(),
                    "last_failure_lesson_agent": agent_id or "system",
                },
            )
        try:
            _bg_pool.submit(
                _record_failure_lesson_bg,
                task_description,
                failure_reason[:800],
                agent_id or "system",
                context[:800],
                db_path=self._db_path,
                attempt_id=attempt_id,
                task_id=task_id,
            )
        except Exception as exc:
            log.debug("failure lesson submit failed for %s: %s", task.get("id"), exc)

    def _merge_dispatch_attempt_metadata(self, attempt_id: str, patch: dict) -> None:
        """Merge metadata fields for a dispatch attempt without clobbering existing keys."""
        _merge_dispatch_attempt_metadata_db(self._db_path, attempt_id, patch)

    @staticmethod
    def _is_transient_dispatch_error(error: str) -> bool:
        err = (error or "").lower()
        return (
            err.startswith("breaker_")
            or "circuit breaker open" in err
            or "backend_cooldown" in err
            or "probe_in_flight" in err
            or "cli slots busy" in err
            or ("slot" in err and "busy" in err)
            or "all cli backends exhausted" in err
            or "gatewaytransporterror" in err
            or "gatewayclientrequest" in err
            or "gateway closed" in err
            or "timeout" in err
            or "process timeout" in err
            or "rate limit" in err
            or "rate_limit" in err
            or "rate-limited" in err
            or "not registered" in err
            or "unknown agent" in err
        )

    @staticmethod
    def _retry_delay_for_dispatch_error(error: str) -> int:
        err = error or ""
        marker = "cooldown "
        if marker in err:
            try:
                suffix = err.split(marker, 1)[1]
                number = suffix.split("s", 1)[0].strip()
                return max(300, int(float(number)))
            except Exception:
                pass
        if "half_open_probing" in err or "probe_in_flight" in err:
            return 300
        if "backend_cooldown" in err:
            return 300
        return 600

    def process_ready(self) -> list[dict]:
        """Dispatch ready tasks (approved, deps met) to their assigned Hermes profiles.

        Transitions each task: approved → running, dispatches to agent,
        then running → completed/failed based on result.
        Returns list of completed/failed task dicts.
        """
        import sys as _sys
        from pathlib import Path as _Path

        _bc = str(_Path(__file__).parent)
        if _bc not in _sys.path:
            _sys.path.insert(0, _bc)

        ready = self.get_ready_tasks()
        if not ready:
            return []

        from cli_llm import dispatch

        # Phase 5 autonomy gate
        try:
            from autonomy import authorize as _autonomy_authorize
        except Exception:
            _autonomy_authorize = None  # type: ignore[assignment]

        MAX_DISPATCH_PER_TICK = 5
        dispatched = 0
        results = []
        for task in ready:
            if dispatched >= MAX_DISPATCH_PER_TICK:
                break
            tid = task["id"]
            agent = task.get("assigned_agent", "jenna")
            title = task.get("title", "")
            desc = task.get("description", "")

            # brain_cli is a label, not a real OpenClaw agent. These tasks
            # belong to review_task_dispatcher (daily cli_llm path). Skip so
            # the 30s executor doesn't race the daily cron and burn them
            # against a non-existent OpenClaw agent.
            if agent == "brain_cli":
                continue

            # Phase 5: gate dispatch via autonomy.authorize("task.dispatch")
            if _autonomy_authorize is not None:
                gate = _autonomy_authorize("task.dispatch", context={"task_id": tid, "agent": agent})
                if not gate.allowed:
                    log.info(
                        "autonomy gate blocked task.dispatch for %s: %s",
                        tid,
                        gate.reason,
                    )
                    continue
                if gate.requires_ack:
                    # L1: leave in approved state, surface to escalation queue
                    log.debug("autonomy L1 — task %s pending human ack", tid)
                    continue

            # Start the task
            try:
                self.start_task(tid, by="executor")
            except ValueError as exc:
                log.warning("cannot start task %s: %s", tid, exc)
                continue

            # Inject past heuristics into prompt
            heuristic_context, retrieved_heuristic_ids = self._get_relevant_heuristics(title + " " + desc)

            # Inject proven procedures (successful workflows) + lessons (past failures)
            procedure_context, retrieved_procedure_ids = self._get_relevant_procedures_with_ids(
                title + " " + desc
            )
            lesson_context, retrieved_lesson_ids = self._get_relevant_lessons_with_ids(
                title + " " + desc,
                agent,
            )
            if retrieved_procedure_ids:
                self._merge_task_metadata(tid, {"retrieved_procedure_ids": retrieved_procedure_ids})
            if retrieved_lesson_ids:
                self._merge_task_metadata(tid, {"retrieved_lesson_ids": retrieved_lesson_ids})

            # Dispatch through subscription CLI first; OpenClaw is only a fallback/integration lane.
            prompt = f"Execute this task:\n\nTitle: {title}\nDescription: {desc}"
            if heuristic_context:
                prompt += f"\n\nRelevant heuristics from past tasks:\n{heuristic_context}"
            if procedure_context:
                prompt += (
                    f"\n\nProven procedures for similar work (follow if applicable):\n{procedure_context}"
                )
            if lesson_context:
                prompt += f"\n\nPast failures to AVOID (honor strictly):\n{lesson_context}"
            prompt += "\n\nDo the work and report the result concisely."

            domain = (task.get("metadata") or {}).get("domain", "general")
            deferred = False
            attempt = self.record_dispatch_attempt_start(
                tid,
                agent=agent,
                backend="cli_llm",
                prompt_chars=len(prompt),
                metadata={
                    "source": "process_ready",
                    "procedure_ids": retrieved_procedure_ids,
                    "lesson_ids": retrieved_lesson_ids,
                },
            )
            try:
                result = dispatch(
                    agent=agent,
                    message=prompt,
                    thinking="medium",
                    timeout=120,
                    openclaw_agent=agent,
                )
                if result.ok and result.text:
                    self.finish_dispatch_attempt(
                        attempt["id"],
                        status="completed",
                        backend=getattr(result, "backend", ""),
                        model=getattr(result, "model", ""),
                        result_preview=result.text[:500],
                        response_chars=len(result.text),
                        duration_ms=getattr(result, "duration_ms", 0),
                        metadata={
                            "attempts": getattr(result, "attempts", 0),
                            "provider": getattr(result, "provider", ""),
                        },
                    )
                    updated = self.complete_task(tid, result=result.text[:1000], by="executor")
                    log.info("task %s completed by %s", tid, agent)
                    # Reinforce retrieved heuristics that helped (MemRL pattern)
                    for hid in retrieved_heuristic_ids:
                        try:
                            from entity_graph import reinforce_memory

                            reinforce_memory(hid, success=True)
                        except Exception:
                            pass
                    # Extract heuristic + procedure in capped background pool (don't block tick)
                    _result_text = result.text[:1000]
                    _bg_pool.submit(
                        lambda t=task, r=_result_text, d=dispatch: (
                            self._extract_heuristic(t, r, d),
                            self._extract_procedure(t, r, d),
                        )
                    )
                else:
                    error_msg = result.error or "agent returned empty response"
                    if self._is_transient_dispatch_error(error_msg):
                        self.finish_dispatch_attempt(
                            attempt["id"],
                            status="deferred",
                            backend=getattr(result, "backend", ""),
                            model=getattr(result, "model", ""),
                            error_class="transient_dispatch",
                            error=error_msg,
                            duration_ms=getattr(result, "duration_ms", 0),
                            metadata={
                                "attempts": getattr(result, "attempts", 0),
                                "rate_limited": getattr(result, "rate_limited", False),
                            },
                        )
                        updated = self.defer_task(
                            tid,
                            error=error_msg[:500],
                            retry_after_s=self._retry_delay_for_dispatch_error(error_msg),
                            by="executor_defer",
                        )
                        deferred = True
                        self._record_failure_lesson_async(
                            task,
                            error_msg,
                            agent,
                            context="status=deferred; error_class=transient_dispatch",
                            attempt_id=attempt["id"],
                        )
                        log.warning(
                            "task %s deferred after transient dispatch error: %s",
                            tid,
                            error_msg[:200],
                        )
                    else:
                        self.finish_dispatch_attempt(
                            attempt["id"],
                            status="failed",
                            backend=getattr(result, "backend", ""),
                            model=getattr(result, "model", ""),
                            error_class="terminal_dispatch",
                            error=error_msg,
                            duration_ms=getattr(result, "duration_ms", 0),
                            metadata={
                                "attempts": getattr(result, "attempts", 0),
                                "rate_limited": getattr(result, "rate_limited", False),
                            },
                        )
                        updated = self.fail_task(tid, error=error_msg[:500], by="executor")
                        self._record_failure_lesson_async(
                            task,
                            error_msg,
                            agent,
                            context="status=failed; error_class=terminal_dispatch",
                            attempt_id=attempt["id"],
                        )
                        log.warning("task %s failed: %s", tid, error_msg[:200])
            except Exception as exc:
                self.finish_dispatch_attempt(
                    attempt["id"],
                    status="failed",
                    error_class="exception",
                    error=str(exc),
                )
                try:
                    updated = self.fail_task(tid, error=str(exc)[:500], by="executor")
                except ValueError:
                    updated = self.get_task(tid) or {}
                self._record_failure_lesson_async(
                    task,
                    str(exc),
                    agent,
                    context="status=failed; error_class=exception",
                    attempt_id=attempt["id"],
                )
                log.warning("task %s dispatch error: %s", tid, exc)

            # Record outcome for accuracy tracking
            # Failed tasks count as incorrect (chris_override=True) since the brain's
            # delegation/confidence was wrong — the task couldn't be completed
            if deferred:
                results.append(updated)
                dispatched += 1
                continue
            task_failed = updated.get("status") != "completed"
            try:
                self.record_outcome(
                    task_id=tid,
                    domain=domain,
                    brain_recommendation=task.get("confidence_reasoning", ""),
                    actual_action=(updated.get("result") or updated.get("error") or "")[:500],
                    chris_override=task_failed,
                    override_reason="agent execution failed" if task_failed else "",
                    procedure_ids=retrieved_procedure_ids,
                    lesson_ids=retrieved_lesson_ids,
                )
            except Exception:
                log.warning("outcome recording failed for %s", tid)

            results.append(updated)
            dispatched += 1

        return results

    # ── Goals ────────────────────────────────────────────────

    def create_goal(
        self,
        title: str,
        description: str = "",
        created_by: str = "chris",
        metadata: dict | None = None,
    ) -> dict:
        now = self._now()
        goal_id = self._gen_id("goal")
        conn = self._conn()
        conn.execute(
            """INSERT INTO goals (id, title, description, status, created_by,
               created_at, updated_at, metadata)
               VALUES (?, ?, ?, 'active', ?, ?, ?, ?)""",
            (goal_id, title, description, created_by, now, now, json.dumps(metadata or {})),
        )
        conn.commit()
        log.info("created goal %s: %s", goal_id, title)
        return self.get_goal(goal_id)  # type: ignore[return-value]

    def get_goal(self, goal_id: str) -> dict | None:
        conn = self._conn()
        row = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        if "metadata" in d and isinstance(d["metadata"], str):
            try:
                d["metadata"] = json.loads(d["metadata"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d

    def list_goals(self, status: str | None = None) -> list[dict]:
        conn = self._conn()
        if status:
            rows = conn.execute(
                "SELECT * FROM goals WHERE status = ? ORDER BY created_at DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM goals ORDER BY created_at DESC").fetchall()
        results = []
        for row in rows:
            d = dict(row)
            if "metadata" in d and isinstance(d["metadata"], str):
                try:
                    d["metadata"] = json.loads(d["metadata"])
                except (json.JSONDecodeError, TypeError):
                    pass
            results.append(d)
        return results

    def complete_goal(self, goal_id: str, by: str = "system") -> dict:
        """Mark a goal as completed. Delegates to update_goal_status for consistent transition enforcement."""
        return self.update_goal_status(goal_id, "completed", by=by)

    # Valid goal status transitions (forward-only)
    _GOAL_TRANSITIONS: dict[str, set[str]] = {
        "active": {"completed", "cancelled"},
        "completed": set(),
        "cancelled": set(),
    }

    def update_goal_status(self, goal_id: str, status: str, by: str = "system") -> dict:
        """Update goal status. Forward-only: active -> completed|cancelled."""
        if status not in ("active", "completed", "cancelled"):
            raise ValueError(f"invalid goal status: {status}")
        goal = self.get_goal(goal_id)
        if goal is None:
            raise ValueError(f"goal {goal_id} not found")
        current = goal["status"]
        allowed = self._GOAL_TRANSITIONS.get(current, set())
        if status not in allowed:
            raise ValueError(
                f"cannot transition goal {goal_id} from '{current}' to '{status}' "
                f"(allowed: {allowed or 'none — terminal state'})"
            )
        conn = self._conn()
        now = self._now()
        updates = {"status": status, "updated_at": now}
        if status in ("completed", "cancelled"):
            updates["completed_at"] = now
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        vals = list(updates.values()) + [goal_id, current]
        cursor = conn.execute(
            f"UPDATE goals SET {set_clause} WHERE id = ? AND status = ?",
            vals,
        )
        conn.commit()
        if cursor.rowcount == 0:
            # Re-read to get actual status for error message
            fresh = self.get_goal(goal_id)
            actual = fresh["status"] if fresh else "unknown"
            raise ValueError(
                f"concurrent update: goal {goal_id} status changed to '{actual}' "
                f"before transition to '{status}' could complete"
            )
        log.info("goal %s -> %s (by %s)", goal_id, status, by)
        return self.get_goal(goal_id)  # type: ignore[return-value]

    # ── Outcomes ─────────────────────────────────────────────

    def record_outcome(
        self,
        task_id: str,
        domain: str = "general",
        brain_recommendation: str = "",
        actual_action: str = "",
        chris_override: bool = False,
        override_reason: str = "",
        procedure_ids: list[str] | None = None,
        lesson_ids: list[str] | None = None,
    ) -> None:
        task = self.get_task(task_id)
        confidence_was = task["confidence"] if task else 0.0
        if procedure_ids is None and task:
            meta = task.get("metadata") or {}
            if isinstance(meta, dict) and isinstance(meta.get("retrieved_procedure_ids"), list):
                procedure_ids = [str(v) for v in meta.get("retrieved_procedure_ids") or []]
        if lesson_ids is None and task:
            meta = task.get("metadata") or {}
            if isinstance(meta, dict) and isinstance(meta.get("retrieved_lesson_ids"), list):
                lesson_ids = [str(v) for v in meta.get("retrieved_lesson_ids") or []]
        procedure_ids = [str(v) for v in (procedure_ids or []) if str(v)]
        lesson_ids = [str(v) for v in (lesson_ids or []) if str(v)]
        now = self._now()
        outcome_id = self._gen_id("outcome")
        conn = self._conn()
        conn.execute(
            """INSERT INTO outcomes
               (id, task_id, domain, brain_recommendation, actual_action,
                chris_override, override_reason, confidence_was, procedure_ids, lesson_ids, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                outcome_id,
                task_id,
                domain,
                brain_recommendation,
                actual_action,
                int(chris_override),
                override_reason,
                confidence_was,
                json.dumps(procedure_ids),
                json.dumps(lesson_ids),
                now,
            ),
        )
        # Update accuracy tracker
        conn.execute(
            """INSERT INTO accuracy_tracker (domain, total_recommendations,
               correct_recommendations, override_count, last_updated)
               VALUES (?, 1, ?, ?, ?)
               ON CONFLICT(domain) DO UPDATE SET
                 total_recommendations = total_recommendations + 1,
                 correct_recommendations = correct_recommendations + ?,
                 override_count = override_count + ?,
                 last_updated = ?""",
            (
                domain,
                0 if chris_override else 1,
                1 if chris_override else 0,
                now,
                0 if chris_override else 1,
                1 if chris_override else 0,
                now,
            ),
        )
        conn.commit()
        try:
            from decision_ledger import resolve_task_decisions

            resolved = resolve_task_decisions(
                task_id,
                actual_outcome=actual_action or override_reason,
                success=not chris_override,
                db_path=self._db_path,
            )
            if resolved:
                log.info("resolved %d pending decision(s) for task %s", resolved, task_id)
        except Exception as exc:
            log.debug("decision ledger outcome resolution failed for %s: %s", task_id, exc)
        log.info("recorded outcome for task %s (override=%s)", task_id, chris_override)

    def list_outcomes(
        self,
        domain: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        conn = self._conn()
        if domain:
            rows = conn.execute(
                "SELECT * FROM outcomes WHERE domain = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (domain, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM outcomes ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        out = [dict(r) for r in rows]
        for row in out:
            if isinstance(row.get("procedure_ids"), str):
                try:
                    row["procedure_ids"] = json.loads(row["procedure_ids"])
                except (json.JSONDecodeError, TypeError):
                    row["procedure_ids"] = []
            if isinstance(row.get("lesson_ids"), str):
                try:
                    row["lesson_ids"] = json.loads(row["lesson_ids"])
                except (json.JSONDecodeError, TypeError):
                    row["lesson_ids"] = []
        return out

    def _extract_heuristic(self, task: dict, result_text: str, dispatch_fn) -> None:
        """ERL-inspired: extract a reusable heuristic from a completed task."""
        try:
            title = task.get("title", "")
            agent = task.get("assigned_agent", "")
            prompt = (
                f"A task was completed successfully.\n"
                f"Task: {title}\nAgent: {agent}\nResult: {result_text[:500]}\n\n"
                f"Extract ONE reusable heuristic in this exact format:\n"
                f"IF [trigger condition] THEN [recommended action] BECAUSE [evidence from this task]\n\n"
                f"Respond with ONLY the heuristic line, nothing else."
            )
            resp = dispatch_fn(agent="sage", message=prompt, thinking="low", timeout=30)
            if resp.ok and resp.text and len(resp.text.strip()) > 20:
                heuristic = resp.text.strip()[:300]
                from indexer import get_embedding
                from vector_store import get_vector_store

                import hashlib

                h_id = f"heuristic:{hashlib.md5(heuristic.encode()).hexdigest()[:16]}"
                emb = get_embedding(heuristic[:1000], prefix="passage")
                if emb:
                    get_vector_store().upsert(
                        "semantic_memory",
                        ids=[h_id],
                        vectors=[emb],
                        documents=[heuristic],
                        payloads=[
                            {
                                "category": "heuristic",
                                "agent": agent,
                                "source": "erl_extraction",
                                "type": "self_learning",
                                "created_at": self._now(),
                            }
                        ],
                    )
                    log.info("extracted heuristic for task %s: %s", task["id"], heuristic[:80])
        except Exception as e:
            log.warning("heuristic extraction failed for %s: %s", task.get("id"), e)

    def _get_relevant_heuristics(self, task_description: str, limit: int = 5) -> tuple[str, list[str]]:
        """Retrieve past heuristics relevant to this task (Reflexion pattern).
        Returns (context_text, retrieved_ids) for utility reinforcement."""
        retrieved_ids: list[str] = []
        try:
            from indexer import get_embedding
            from vector_store import get_vector_store

            query_emb = get_embedding(task_description[:500], prefix="query")
            if not query_emb:
                return "", []
            hits = get_vector_store().query(
                "semantic_memory",
                vector=query_emb,
                k=limit,
                filter={"category": {"$eq": "heuristic"}},
                with_payload=True,
            )
            retrieved_ids = [h.id for h in hits if h.id]
            docs = [h.document for h in hits if h.document]
            if not docs:
                return "", retrieved_ids
            return "\n".join(f"- {d}" for d in docs), retrieved_ids
        except Exception:
            return "", []

    def _get_relevant_procedures(self, task_description: str, limit: int = 2) -> str:
        context, _ids = self._get_relevant_procedures_with_ids(task_description, limit=limit)
        return context

    def _get_relevant_procedures_with_ids(
        self, task_description: str, limit: int = 2
    ) -> tuple[str, list[str]]:
        """Retrieve procedures whose task_type+title share meaningful words with the task.

        Simple word-overlap scorer (cheap, no embedding call). Procedures are ranked
        server-side by success_count DESC so the top-20 window is already warm hits.
        """
        try:
            words = {w.lower() for w in task_description.split() if len(w) > 3}
            if not words:
                return "", []
            conn = self._conn()
            rows = conn.execute(
                "SELECT id, task_type, title, steps, success_count FROM procedures "
                "ORDER BY success_count DESC, last_used DESC LIMIT 20"
            ).fetchall()
            scored = []
            for r in rows:
                pt_text = (r["task_type"] or "").replace("_", " ")
                pt = {w.lower() for w in (pt_text + " " + (r["title"] or "")).split() if len(w) > 3}
                overlap = len(words & pt)
                if overlap >= 2:
                    scored.append((overlap, int(r["success_count"] or 1), r))
            scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
            top = scored[:limit]
            if not top:
                return "", []
            lines = []
            ids = []
            for _, _, r in top:
                ids.append(str(r["id"]))
                try:
                    steps = json.loads(r["steps"]) if isinstance(r["steps"], str) else r["steps"]
                except (json.JSONDecodeError, TypeError):
                    steps = []
                step_preview = "; ".join((steps or [])[:4])
                lines.append(
                    f"- [{r['task_type']}] {r['title']} (used {r['success_count']}x): {step_preview}"
                )
            return "\n".join(lines), ids
        except Exception:
            return "", []

    def _get_relevant_lessons_with_ids(
        self,
        task_description: str,
        agent_id: str,
        limit: int = 2,
    ) -> tuple[str, list[str]]:
        """Retrieve similar past failure lessons for this agent (Reflexion pattern).

        Delegates to failure_memory.get_similar_lessons which uses Jaro-Winkler
        similarity via Neo4j APOC. Formats avoid + try_next prominently.
        """
        try:
            import failure_memory

            lessons = failure_memory.get_similar_lessons(
                task_description,
                agent_id=agent_id,
                limit=limit,
            )
            if not lessons:
                return "", []
            lines = []
            ids: list[str] = []
            for lesson in lessons:
                lesson_id = str(lesson.get("id") or lesson.get("lesson_id") or "").strip()
                if lesson_id:
                    ids.append(lesson_id)
                fragment = (lesson.get("reflection") or lesson.get("task") or "")[:140]
                avoid = (lesson.get("avoid") or "").strip()
                try_next = (lesson.get("try_next") or "").strip()
                line = f"- {fragment}"
                if avoid:
                    line += f" [AVOID: {avoid[:100]}]"
                if try_next:
                    line += f" [TRY_NEXT: {try_next[:100]}]"
                lines.append(line)
            return "\n".join(lines), ids
        except Exception:
            return "", []

    def _get_relevant_lessons(self, task_description: str, agent_id: str, limit: int = 2) -> str:
        """Backward-compatible text-only Reflexion retrieval."""

        try:
            context, _ids = self._get_relevant_lessons_with_ids(
                task_description,
                agent_id,
                limit=limit,
            )
            return context
        except Exception:
            return ""

    # ── Procedural memory ─────────────────────────────────────

    def _maybe_materialize_procedure(self, proc_id: str) -> None:
        """Submit SKILL.md materialization to bg pool for a procedure.

        Voyager/Hermes-style auto-skill creation: once a procedure is proven
        (success_count ≥ 2 in skill_materializer), write it out as a SKILL.md
        for Claude Code + OpenClaw so the agents can discover and invoke it.
        Runs in background so procedure writes don't block on filesystem I/O.
        """
        try:
            conn = self._conn()
            row = conn.execute("SELECT * FROM procedures WHERE id = ?", (proc_id,)).fetchone()
            if not row:
                return
            proc_dict = dict(row)
            for key in ("steps", "tools"):
                v = proc_dict.get(key)
                if isinstance(v, str):
                    try:
                        proc_dict[key] = json.loads(v)
                    except (json.JSONDecodeError, TypeError):
                        pass
            _bg_pool.submit(_materialize_proc_bg, proc_dict)
        except Exception:
            pass

    def _store_procedure(
        self,
        task_type: str,
        title: str,
        steps: list[str],
        preconditions: str = "",
        tools: list | None = None,
        source: str = "extraction",
    ) -> str:
        """Store or deduplicate a procedure. Returns procedure ID."""
        conn = self._conn()
        now = self._now()
        step_tokens = set(" ".join(steps).lower().split())

        # Check existing procedures with same task_type for dedup
        rows = conn.execute("SELECT id, steps FROM procedures WHERE task_type = ?", (task_type,)).fetchall()
        for row in rows:
            try:
                existing_steps = json.loads(row["steps"])
            except (json.JSONDecodeError, TypeError):
                continue
            existing_tokens = set(" ".join(existing_steps).lower().split())
            union = step_tokens | existing_tokens
            if not union:
                continue
            jaccard = len(step_tokens & existing_tokens) / len(union)
            if jaccard > 0.70:
                conn.execute(
                    "UPDATE procedures SET success_count = success_count + 1, last_used = ? WHERE id = ?",
                    (now, row["id"]),
                )
                conn.commit()
                log.debug("deduped procedure %s (jaccard=%.2f)", row["id"], jaccard)
                self._maybe_materialize_procedure(row["id"])
                return row["id"]

        # Insert new procedure
        proc_id = self._gen_id("proc")
        conn.execute(
            """INSERT INTO procedures
               (id, task_type, title, steps, preconditions, tools, success_count,
                last_used, created_at, source)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
            (
                proc_id,
                task_type,
                title,
                json.dumps(steps),
                preconditions,
                json.dumps(tools or []),
                now,
                now,
                source,
            ),
        )
        conn.commit()
        log.info("stored procedure %s: %s (%d steps)", proc_id, task_type, len(steps))
        self._maybe_materialize_procedure(proc_id)
        return proc_id

    def get_procedures(
        self,
        task_type: str | None = None,
        source: str | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Retrieve procedures, ordered by success_count DESC, last_used DESC."""
        clauses: list[str] = []
        params: list = []
        if task_type:
            clauses.append("task_type = ?")
            params.append(task_type)
        if source:
            clauses.append("source = ?")
            params.append(source)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        conn = self._conn()
        rows = conn.execute(
            f"SELECT * FROM procedures{where} ORDER BY success_count DESC, last_used DESC LIMIT ?",
            params,
        ).fetchall()
        results = []
        for row in rows:
            d = dict(row)
            for key in ("steps", "tools"):
                if key in d and isinstance(d[key], str):
                    try:
                        d[key] = json.loads(d[key])
                    except (json.JSONDecodeError, TypeError):
                        pass
            results.append(d)
        return results

    def _extract_procedure(self, task: dict, result_text: str, dispatch_fn) -> None:
        """CoALA/Voyager pattern: extract reusable multi-step procedures from successful tasks."""
        if len(result_text) < 100:
            return  # too short to contain a useful procedure
        try:
            title = task.get("title", "")
            agent = task.get("assigned_agent", "")
            prompt = (
                f"A multi-step task was completed successfully.\n"
                f"Task: {title}\nAgent: {agent}\nResult: {result_text[:800]}\n\n"
                "If this result describes a multi-step procedure (3+ steps), "
                "extract it as a reusable template.\n"
                f"Return ONLY a JSON object (or null if no procedure):\n"
                '{"task_type": "...", "steps": ["step 1", "step 2", ...], '
                '"preconditions": "...", "tools_used": ["..."]}'
            )
            resp = dispatch_fn(agent="sage", message=prompt, thinking="low", timeout=30)
            if not resp.ok or not resp.text or resp.text.strip()[:4] == "null":
                return
            text = resp.text.strip()
            import re as _re

            text = _re.sub(r"^```(?:json)?\s*", "", text)
            text = _re.sub(r"\s*```$", "", text)
            data = json.loads(text.strip())
            steps = data.get("steps", [])
            if len(steps) < 3:
                return  # not a multi-step procedure

            procedure_text = f"Procedure: {data.get('task_type', title)}\nSteps:\n" + "\n".join(
                f"  {i+1}. {s}" for i, s in enumerate(steps)
            )
            if data.get("preconditions"):
                procedure_text += f"\nPreconditions: {data['preconditions']}"

            from indexer import get_embedding
            from vector_store import get_vector_store

            import hashlib

            p_id = f"proc:{hashlib.md5(procedure_text[:200].encode()).hexdigest()[:16]}"
            emb = get_embedding(procedure_text[:1000], prefix="passage")
            if emb:
                get_vector_store().upsert(
                    "patterns",
                    ids=[p_id],
                    vectors=[emb],
                    documents=[procedure_text],
                    payloads=[
                        {
                            "type": "procedure",
                            "agent": agent,
                            "source": "voyager_extraction",
                            "created_at": self._now(),
                        }
                    ],
                )
                log.info("extracted procedure for task %s (%d steps)", task["id"], len(steps))
            # Structured storage for typed retrieval and dedup
            try:
                self._store_procedure(
                    task_type=data.get("task_type", title),
                    title=title,
                    steps=steps,
                    preconditions=data.get("preconditions", ""),
                    tools=data.get("tools_used", []),
                    source="extraction",
                )
            except Exception as e:
                log.warning("procedure store failed: %s", e)
        except Exception as e:
            log.warning("procedure extraction failed for %s: %s", task.get("id"), e)

    def suggest_delegation_learned(self, task_description: str) -> dict | None:
        """Learned routing: find most similar past successful task, route to same agent.

        Returns {"agent": str, "confidence": float, "reasoning": str} or None if
        insufficient outcome data (<10 outcomes).
        """
        conn = self._conn()
        outcomes = conn.execute(
            "SELECT o.*, t.title, t.assigned_agent FROM outcomes o "
            "JOIN tasks t ON o.task_id = t.id "
            "WHERE o.chris_override = 0 ORDER BY o.created_at DESC LIMIT 100"
        ).fetchall()
        if len(outcomes) < 10:
            return None  # not enough data for learned routing

        # Token-overlap similarity (cheap, no LLM call)
        try:
            best_agent = None
            best_score = -1
            for out in outcomes:
                out_dict = dict(out)
                past_title = out_dict.get("title", "")
                # Cheap token overlap (no LLM call)
                query_tokens = set(task_description.lower().split())
                past_tokens = set(past_title.lower().split())
                overlap = len(query_tokens & past_tokens) / max(len(query_tokens | past_tokens), 1)
                if overlap > best_score:
                    best_score = overlap
                    best_agent = out_dict.get("assigned_agent")

            if best_agent and best_score > 0.3:
                return {
                    "agent": best_agent,
                    "confidence": min(0.9, 0.5 + best_score),
                    "reasoning": (
                        "Learned routing: similar past task succeeded with "
                        f"agent={best_agent} (overlap={best_score:.0%})"
                    ),
                }
        except Exception:
            pass
        return None

    def get_domain_accuracy(self, domain: str | None = None) -> dict:
        conn = self._conn()
        if domain:
            rows = conn.execute("SELECT * FROM accuracy_tracker WHERE domain = ?", (domain,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM accuracy_tracker").fetchall()
        result = {}
        for row in rows:
            d = dict(row)
            total = d["total_recommendations"]
            correct = d["correct_recommendations"]
            d["accuracy"] = round(correct / total, 3) if total else 0.0
            result[d["domain"]] = d
        return result


# ── Module-level singleton ───────────────────────────────────

try:
    from config import BRAIN_LOGS_DIR
except ImportError:
    BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")

task_queue = TaskQueue(BRAIN_LOGS_DIR / "autonomy.db")
