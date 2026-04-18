"""brain_core/autopilot.py — global autopilot toggle.

Phase F2 (Brain v2): state lives in autonomy.db `brain_config` (key prefix
`autopilot.*`). The legacy JSON file at logs/autopilot_state.json is read as
a one-shot fallback to seed brain_config if it exists, then never read again.

Single writer, single source of truth, all routed through `brain_config_store`.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

try:
    import brain_config_store
    from config import BRAIN_LOGS_DIR
except ImportError:
    BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")
    brain_config_store = None  # type: ignore[assignment]

from safe_state import load_state, save_state

STATE_FILE = BRAIN_LOGS_DIR / "autopilot_state.json"

_DEFAULTS = {
    "enabled": False,
    "confidence_threshold": 0.8,
    "updated_at": "",
    "updated_by": "system",
}


def _read_from_brain_config() -> dict | None:
    if brain_config_store is None:
        return None
    rows = brain_config_store.get_prefix("autopilot.")
    if not rows:
        return None
    state: dict = dict(_DEFAULTS)
    if "autopilot.enabled" in rows:
        state["enabled"] = rows["autopilot.enabled"] == "true"
    if "autopilot.confidence_threshold" in rows:
        try:
            state["confidence_threshold"] = float(rows["autopilot.confidence_threshold"])
        except (TypeError, ValueError):
            pass
    return state


def _write_to_brain_config(enabled: bool, threshold: float, updated_by: str) -> None:
    if brain_config_store is None:
        return
    brain_config_store.set("autopilot.enabled", "true" if enabled else "false", updated_by=updated_by)
    brain_config_store.set("autopilot.confidence_threshold", str(threshold), updated_by=updated_by)


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
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
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
