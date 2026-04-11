"""brain_core/autopilot.py — global autopilot toggle.

Source of truth for all autonomous behavior. State persisted in
logs/autopilot_state.json via the safe_state atomic-write pattern.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

try:
    from config import BRAIN_LOGS_DIR
except ImportError:
    BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")

from safe_state import load_state, save_state

STATE_FILE = BRAIN_LOGS_DIR / "autopilot_state.json"

_DEFAULTS = {
    "enabled": False,
    "confidence_threshold": 0.8,
    "updated_at": "",
    "updated_by": "system",
}


def get_state() -> dict:
    """Read autopilot state from disk, returning defaults if missing."""
    raw = load_state(STATE_FILE)
    if not raw:
        return dict(_DEFAULTS)
    merged = dict(_DEFAULTS)
    merged.update(raw)
    return merged


def set_state(
    enabled: bool,
    threshold: float = 0.8,
    updated_by: str = "api",
) -> dict:
    """Write autopilot state to disk. Returns the new state."""
    state = {
        "enabled": enabled,
        "confidence_threshold": threshold,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "updated_by": updated_by,
    }
    save_state(STATE_FILE, state)
    return state


def is_enabled() -> bool:
    """Shortcut: is autopilot currently on?"""
    return get_state()["enabled"]


def should_auto_approve(confidence: float) -> bool:
    """True when autopilot is enabled AND confidence meets the threshold."""
    st = get_state()
    return st["enabled"] and confidence >= st["confidence_threshold"]
