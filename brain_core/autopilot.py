"""brain_core/autopilot.py — global autopilot toggle.

Phase F2 (Brain v2): state migrated from logs/autopilot_state.json to
brain_config table in autonomy.db (key prefix `autopilot.*`). The JSON file
is read as a one-shot fallback to seed brain_config if it exists, then never
read again. Writes go to brain_config only.

Single writer, single source of truth. The JSON file is harmless to leave
on disk but no longer the truth layer.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

try:
    from config import AUTONOMY_DB, BRAIN_LOGS_DIR
except ImportError:
    BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")
    AUTONOMY_DB = BRAIN_LOGS_DIR / "autonomy.db"

from safe_state import load_state, save_state  # noqa: F401  (kept for back-compat callers)

STATE_FILE = BRAIN_LOGS_DIR / "autopilot_state.json"

_DEFAULTS = {
    "enabled": False,
    "confidence_threshold": 0.8,
    "updated_at": "",
    "updated_by": "system",
}


def _ensure_brain_config_schema() -> None:
    AUTONOMY_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(AUTONOMY_DB))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS brain_config (
                key TEXT PRIMARY KEY, value TEXT NOT NULL,
                updated_at TEXT NOT NULL, updated_by TEXT DEFAULT 'system')"""
        )
        conn.commit()
    finally:
        conn.close()


def _read_from_brain_config() -> dict | None:
    _ensure_brain_config_schema()
    conn = sqlite3.connect(str(AUTONOMY_DB))
    try:
        rows = conn.execute(
            "SELECT key, value, updated_at, updated_by FROM brain_config "
            "WHERE key LIKE 'autopilot.%'"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    state: dict = dict(_DEFAULTS)
    for key, value, updated_at, updated_by in rows:
        short = key[len("autopilot.") :]
        if short == "enabled":
            state["enabled"] = value == "true"
            state["updated_at"] = updated_at
            state["updated_by"] = updated_by
        elif short == "confidence_threshold":
            try:
                state["confidence_threshold"] = float(value)
            except (TypeError, ValueError):
                pass
    return state


def _write_to_brain_config(enabled: bool, threshold: float, updated_by: str) -> None:
    _ensure_brain_config_schema()
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = sqlite3.connect(str(AUTONOMY_DB))
    try:
        for key, value in (
            ("autopilot.enabled", "true" if enabled else "false"),
            ("autopilot.confidence_threshold", str(threshold)),
        ):
            conn.execute(
                "INSERT INTO brain_config (key, value, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                (key, value, now_iso, updated_by),
            )
        conn.commit()
    finally:
        conn.close()


def _migrate_json_to_brain_config_once() -> None:
    """One-shot bootstrap: if brain_config has no autopilot rows but the legacy
    JSON file does, copy the JSON state into brain_config, then leave the JSON
    file in place (read-only) for rollback safety.
    """
    if _read_from_brain_config() is not None:
        return
    raw = load_state(STATE_FILE)
    if not raw:
        return
    enabled = bool(raw.get("enabled", False))
    threshold = float(raw.get("confidence_threshold", 0.8))
    updated_by = raw.get("updated_by", "json_migration")
    _write_to_brain_config(enabled, threshold, updated_by)


def get_state() -> dict:
    """Read autopilot state from brain_config, falling back to JSON once."""
    _migrate_json_to_brain_config_once()
    state = _read_from_brain_config()
    if state is None:
        return dict(_DEFAULTS)
    return state


def set_state(
    enabled: bool,
    threshold: float = 0.8,
    updated_by: str = "api",
) -> dict:
    """Write autopilot state to brain_config. Returns the new state.

    Also writes to the legacy JSON file for one rollback cycle. After Phase F
    soak window the JSON write can be deleted.
    """
    _write_to_brain_config(enabled, threshold, updated_by)
    legacy_state = {
        "enabled": enabled,
        "confidence_threshold": threshold,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "updated_by": updated_by,
    }
    try:
        save_state(STATE_FILE, legacy_state)
    except Exception:
        pass
    return legacy_state


def is_enabled() -> bool:
    """Shortcut: is autopilot currently on?"""
    return get_state()["enabled"]


def should_auto_approve(confidence: float) -> bool:
    """True when autopilot is enabled AND confidence meets the threshold."""
    st = get_state()
    return st["enabled"] and confidence >= st["confidence_threshold"]
