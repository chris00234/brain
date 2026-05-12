"""brain_core/interoception.py — D8 body-state awareness.

Biological motivation: the insula integrates internal body signals
(heartbeat, breath, fatigue, hunger) into cognition. Decisions and
attention are colored by interoceptive state — a tired brain retrieves
different memories than a fresh one.

This module surfaces Chris's most recent body data so other brain
subsystems (attention.py recall modulation, predictive.py prefetch,
brain_loop drives) can condition their behavior on his actual state.

Data path:
  iPhone Shortcut → iCloud Drive JSON → apple_health.py ingest →
  this module reads either iCloud JSONs OR brain canonical health notes.

Read-only, no LLM, no external services. Returns gracefully when no
data exists (state-of-self → {"available": false}).

What consumers can do with this:
  - attention.py: under low-sleep state, prefer canonical-tier
    retrieval (high precision) over experimental cross-domain hops
  - brain_loop.speak: skip non-urgent digests when energy is low
  - predictive.py: boost "rest/recovery" related canonical when
    energy_trend is downward
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

log = logging.getLogger("brain.interoception")

HEALTH_EXPORT_DIR = Path("/Users/chrischo/Library/Mobile Documents/com~apple~CloudDocs/brain/health")
RECENT_DAYS = 7
LOW_SLEEP_THRESHOLD = 6.0  # hours
LOW_ENERGY_KCAL_THRESHOLD = 200.0  # active_kcal/day


def _parse_health_json(path: Path) -> dict | None:
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None

    # Apple Shortcut writes numeric fields as strings; coerce defensively.
    def _f(key: str) -> float | None:
        v = raw.get(key)
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    return {
        "date": raw.get("date"),
        "sleep_hours": _f("sleep_hours"),
        "active_kcal": _f("active_kcal"),
        "resting_hr_bpm": _f("resting_hr_bpm"),
        "hrv_sdnn_ms": _f("hrv_sdnn_ms"),
        "steps": _f("steps"),
        "exercise_min": _f("exercise_min"),
        "stand_hours": _f("stand_hours"),
        "sleep_quality": _f("sleep_quality"),
    }


def _read_recent_days(days: int = RECENT_DAYS) -> list[dict]:
    """Read most recent health JSONs from iCloud. Deduplicate by date keeping latest.

    No date cutoff — we want to surface whatever the freshest signal is even
    if it's stale. Consumers read staleness_days from state_of_self() to
    decide whether to trust it. Limit applies only to history depth.
    """
    if not HEALTH_EXPORT_DIR.exists():
        return []
    by_date: dict[str, dict] = {}
    for path in sorted(HEALTH_EXPORT_DIR.glob("*.json")):
        data = _parse_health_json(path)
        if not data or not data.get("date"):
            continue
        existing = by_date.get(data["date"])
        if existing is None or path.stat().st_mtime > existing["_mtime"]:
            data["_mtime"] = path.stat().st_mtime
            by_date[data["date"]] = data
    items = sorted(by_date.values(), key=lambda d: d["date"], reverse=True)
    for item in items:
        item.pop("_mtime", None)
    return items[:days]


def _classify_state(latest: dict | None, history: list[dict]) -> dict:
    """Derive a coarse state label from the most recent + recent history."""
    if not latest:
        return {"label": "no_signal", "modifiers": []}

    modifiers: list[str] = []
    sleep = latest.get("sleep_hours")
    kcal = latest.get("active_kcal")
    steps = latest.get("steps")

    if sleep is not None and sleep < LOW_SLEEP_THRESHOLD:
        modifiers.append("low_sleep")
    if kcal is not None and kcal < LOW_ENERGY_KCAL_THRESHOLD:
        modifiers.append("low_activity")
    if steps is not None and steps < 1000:
        modifiers.append("sedentary")

    # Energy trend: compare latest active_kcal to recent median
    kcal_vals = [d["active_kcal"] for d in history if d.get("active_kcal") is not None]
    if kcal is not None and len(kcal_vals) >= 3:
        sorted_vals = sorted(kcal_vals)
        median = sorted_vals[len(sorted_vals) // 2]
        if kcal < median * 0.6:
            modifiers.append("energy_trending_down")
        elif kcal > median * 1.4:
            modifiers.append("energy_trending_up")

    if "low_sleep" in modifiers and "low_activity" in modifiers:
        label = "depleted"
    elif "low_sleep" in modifiers:
        label = "rested_poorly"
    elif modifiers:
        label = "subnormal"
    else:
        label = "nominal"
    return {"label": label, "modifiers": modifiers}


def state_of_self() -> dict:
    """Return Chris's current interoceptive state.

    Output shape:
      {
        "available": bool,
        "latest_date": "YYYY-MM-DD" | None,
        "staleness_days": int,
        "latest": {sleep_hours, active_kcal, steps, ...} | None,
        "state": {label, modifiers},
        "history": [recent N days],
      }
    """
    history = _read_recent_days(RECENT_DAYS)
    latest = history[0] if history else None
    staleness = -1
    if latest and latest.get("date"):
        try:
            d = datetime.fromisoformat(latest["date"]).replace(tzinfo=UTC)
            staleness = (datetime.now(UTC) - d).days
        except (TypeError, ValueError):
            staleness = -1
    return {
        "available": latest is not None,
        "latest_date": latest.get("date") if latest else None,
        "staleness_days": staleness,
        "latest": latest,
        "state": _classify_state(latest, history),
        "history": history,
    }


if __name__ == "__main__":
    print(json.dumps(state_of_self(), indent=2, ensure_ascii=False))  # noqa: T201
