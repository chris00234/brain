"""brain_core/action_triggers.py — declarative rules that auto-create tasks.

Triggers fire only when autopilot is ON. Each trigger defines a condition type,
a match config, and an action template. When a condition matches (and cooldown
has elapsed), a task is created in the queue automatically.

Uses the same autonomy.db as task_queue.

Usage:
    from action_triggers import evaluate_triggers, check_proactive_triggers
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import autopilot
from task_queue import task_queue

try:
    from config import BRAIN_LOGS_DIR
except ImportError:
    BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")

log = logging.getLogger("brain.action_triggers")

DB_PATH = BRAIN_LOGS_DIR / "autonomy.db"

DEFAULT_TRIGGERS = [
    {
        "name": "health_check_failed",
        "description": "Auto-investigate when a health check fails",
        "condition_type": "proactive_insight",
        "condition_config": {"category": "health", "severity": "urgent"},
        "action_template": {
            "title": "Investigate: {summary}",
            "agent": "ellie",
            "priority": 2,
            "confidence": 0.85,
        },
    },
    {
        "name": "meeting_no_prep",
        "description": "Auto-create meeting prep brief",
        "condition_type": "proactive_insight",
        "condition_config": {"category": "schedule", "severity": "warning"},
        "action_template": {
            "title": "Prep brief: {summary}",
            "agent": "jenna",
            "priority": 4,
            "confidence": 0.85,
        },
    },
    {
        "name": "eval_accuracy_drop",
        "description": "Investigate RAG accuracy regression",
        "condition_type": "proactive_insight",
        "condition_config": {"category": "trend", "severity": "warning"},
        "action_template": {
            "title": "Investigate RAG accuracy drop",
            "agent": "ellie",
            "priority": 5,
            "confidence": 0.7,
        },
    },
    {
        "name": "scheduler_failure",
        "description": "Auto-fix scheduler job failures",
        "condition_type": "proactive_insight",
        "condition_config": {"category": "health", "severity": "warning"},
        "action_template": {"title": "Fix: {summary}", "agent": "ellie", "priority": 3, "confidence": 0.8},
    },
    {
        "name": "learned_playbook_info",
        "description": "Run safe read-only playbook when Brain recognizes a repeated Chris pattern",
        "condition_type": "proactive_insight",
        "condition_config": {"category": "playbook", "severity": "info"},
        "action_template": {
            "title": "Execute proactive playbook: {summary}",
            "agent": "sage",
            "priority": 4,
            "confidence": 0.8,
        },
    },
    {
        "name": "learned_playbook_warning",
        "description": "Run higher-priority safe playbook after risky recurring event classes",
        "condition_type": "proactive_insight",
        "condition_config": {"category": "playbook", "severity": "warning"},
        "action_template": {
            "title": "Execute proactive playbook: {summary}",
            "agent": "sage",
            "priority": 3,
            "confidence": 0.85,
        },
    },
]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

from contextlib import contextmanager


@contextmanager
def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _init_db() -> None:
    """Create triggers table and seed defaults if empty."""
    try:
        with _conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS triggers (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT DEFAULT '',
                    condition_type TEXT NOT NULL,
                    condition_config TEXT NOT NULL,
                    action_template TEXT NOT NULL,
                    enabled INTEGER DEFAULT 1,
                    last_fired_at TEXT,
                    fire_count INTEGER DEFAULT 0,
                    cooldown_seconds INTEGER DEFAULT 3600,
                    created_at TEXT NOT NULL
                )
            """)

            count = conn.execute("SELECT COUNT(*) FROM triggers").fetchone()[0]
            if count == 0:
                now = datetime.now(UTC).isoformat(timespec="seconds")
                for t in DEFAULT_TRIGGERS:
                    conn.execute(
                        "INSERT INTO triggers (id, name, description, condition_type, condition_config, action_template, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            str(uuid.uuid4()),
                            t["name"],
                            t.get("description", ""),
                            t["condition_type"],
                            json.dumps(t["condition_config"]),
                            json.dumps(t["action_template"]),
                            now,
                        ),
                    )
                log.info("Seeded %d default triggers", len(DEFAULT_TRIGGERS))
    except Exception as e:
        log.error("_init_db failed: %s", e)


# Ensure table exists on import
_init_db()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _check_cooldown(trigger: dict) -> bool:
    """True if enough time has passed since the trigger last fired."""
    last = trigger.get("last_fired_at")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=UTC)
        elapsed = (datetime.now(UTC) - last_dt).total_seconds()
        return elapsed >= trigger.get("cooldown_seconds", 3600)
    except (ValueError, TypeError):
        return True


def _apply_template(template: str, context: dict) -> str:
    """Simple {key} interpolation. Missing keys left as-is."""
    result = template
    for key, value in context.items():
        result = result.replace("{" + key + "}", str(value))
    return result


def _fire_trigger(trigger: dict, context: dict) -> dict:
    """Create a task from the trigger's action template and update fire stats."""
    tmpl = trigger["action_template"]
    if isinstance(tmpl, str):
        tmpl = json.loads(tmpl)

    title = _apply_template(tmpl.get("title", "Auto-task"), context)
    agent = tmpl.get("agent", "jenna")
    priority = tmpl.get("priority", 5)
    confidence = float(tmpl.get("confidence", 0.0))

    task = task_queue.create_task(
        title=title,
        description=f"Auto-created by trigger '{trigger['name']}'",
        assigned_agent=agent,
        priority=priority,
        confidence=confidence,
    )

    # Update trigger stats (both DB and in-memory to prevent re-fire in same run)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    trigger["last_fired_at"] = now
    try:
        with _conn() as conn:
            conn.execute(
                "UPDATE triggers SET last_fired_at = ?, fire_count = fire_count + 1 WHERE id = ?",
                (now, trigger["id"]),
            )
    except sqlite3.Error as e:
        log.warning("failed to update trigger stats for '%s': %s", trigger["name"], e)

    log.info("Trigger '%s' fired -> task %s: %s", trigger["name"], task["id"], title)
    return task


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def evaluate_triggers() -> list[dict]:
    """Check all enabled triggers. Only fires when autopilot is ON.

    Returns list of created tasks (may be empty).
    """
    if not autopilot.is_enabled():
        return []

    with _conn() as conn:
        rows = conn.execute("SELECT * FROM triggers WHERE enabled = 1").fetchall()

    created: list[dict] = []
    for row in rows:
        trigger = _row_to_dict(row)
        if not _check_cooldown(trigger):
            continue
        # proactive_insight triggers are handled by check_proactive_triggers().
        # This loop handles future condition_types that don't need external input.
        if trigger["condition_type"] == "proactive_insight":
            continue

    return created


def check_proactive_triggers(insights: list) -> list[dict]:
    """Match proactive insights against triggers with condition_type='proactive_insight'.

    Each insight should be a dict (or dataclass with .category/.severity/.summary).
    Returns list of created tasks.
    """
    if not autopilot.is_enabled():
        return []

    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM triggers WHERE enabled = 1 AND condition_type = 'proactive_insight'"
        ).fetchall()

    triggers = [_row_to_dict(row) for row in rows]
    created: list[dict] = []

    for insight in insights:
        # Normalize: support both dicts and dataclass-like objects
        if hasattr(insight, "category"):
            cat = insight.category
            sev = insight.severity
            summary = insight.summary
        elif isinstance(insight, dict):
            cat = insight.get("category", "")
            sev = insight.get("severity", "")
            summary = insight.get("summary", "")
        else:
            continue

        for trigger in triggers:
            cfg = trigger["condition_config"]
            if isinstance(cfg, str):
                cfg = json.loads(cfg)

            if cfg.get("category") != cat:
                continue
            if cfg.get("severity") != sev:
                continue
            if not _check_cooldown(trigger):
                continue

            # Phase 5 autonomy gate
            try:
                from autonomy import authorize as _autonomy_authorize

                kind = f"trigger.fire.{trigger.get('name', 'unknown')}"
                gate = _autonomy_authorize(kind, context={"trigger_id": trigger.get("id")})
                if not gate.allowed:
                    continue
            except Exception:
                pass

            context = {"summary": summary, "category": cat, "severity": sev}
            task = _fire_trigger(trigger, context)
            created.append(task)

    return created


def list_triggers() -> list[dict]:
    """Return all trigger rules."""
    _init_db()
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM triggers ORDER BY name").fetchall()
    return [_row_to_dict(r) for r in rows]


def get_trigger(trigger_id: str) -> dict | None:
    """Fetch one trigger by id. Returns None if not found."""
    _init_db()
    with _conn() as conn:
        row = conn.execute("SELECT * FROM triggers WHERE id = ?", (trigger_id,)).fetchone()
    return _row_to_dict(row) if row else None


def create_trigger(
    *,
    name: str,
    description: str = "",
    condition_type: str,
    condition_config: dict,
    action_template: dict,
    enabled: bool = True,
    cooldown_seconds: int = 3600,
) -> dict:
    """Create a new trigger. Raises ValueError on duplicate name."""
    _init_db()
    if not name or not condition_type:
        raise ValueError("name and condition_type are required")
    trigger_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with _conn() as conn:
        existing = conn.execute("SELECT id FROM triggers WHERE name = ?", (name,)).fetchone()
        if existing:
            raise ValueError(f"trigger with name '{name}' already exists")
        conn.execute(
            "INSERT INTO triggers (id, name, description, condition_type, condition_config, "
            "action_template, enabled, cooldown_seconds, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                trigger_id,
                name,
                description,
                condition_type,
                json.dumps(condition_config),
                json.dumps(action_template),
                1 if enabled else 0,
                cooldown_seconds,
                now,
            ),
        )
    result = get_trigger(trigger_id)
    if result is None:
        raise RuntimeError("trigger create succeeded but readback failed")
    return result


_UPDATABLE_COLUMNS: frozenset[str] = frozenset(
    {"enabled", "cooldown_seconds", "description", "condition_config", "action_template"}
)


def update_trigger(trigger_id: str, **fields: object) -> dict | None:
    """Patch a trigger. Allowed fields: enabled, cooldown_seconds, description,
    condition_config, action_template. Returns updated row or None if missing."""
    updates = {k: v for k, v in fields.items() if k in _UPDATABLE_COLUMNS and v is not None}
    if not updates:
        return get_trigger(trigger_id)
    _init_db()
    set_clauses = []
    params: list[object] = []
    for k, v in updates.items():
        # Defense-in-depth: re-validate the column name at the SQL-building
        # site. The allowlist filter above already gates this, but if a
        # future refactor bypasses it or a stray call uses **untrusted_dict,
        # we still refuse to build SQL with an unknown identifier.
        if k not in _UPDATABLE_COLUMNS:
            raise ValueError(f"update_trigger: refusing column {k!r} (not in allowlist)")
        if k in ("condition_config", "action_template") and isinstance(v, dict):
            v = json.dumps(v)
        elif k == "enabled":
            v = 1 if v else 0
        set_clauses.append(f"{k} = ?")
        params.append(v)
    params.append(trigger_id)
    with _conn() as conn:
        cur = conn.execute(
            f"UPDATE triggers SET {', '.join(set_clauses)} WHERE id = ?",
            params,
        )
        if cur.rowcount == 0:
            return None
    return get_trigger(trigger_id)


def delete_trigger(trigger_id: str) -> bool:
    """Delete a trigger by id. Returns True if deleted, False if not found."""
    _init_db()
    with _conn() as conn:
        cur = conn.execute("DELETE FROM triggers WHERE id = ?", (trigger_id,))
        return cur.rowcount > 0
