"""brain_core/time_decay.py — exponential freshness multiplier.

Some collections should fade with age (messages, session context), others are
durable (preferences, canonical knowledge). This module provides a score
multiplier in [decay_floor, 1.0] based on how old a result's `created_at` is.

Half-lives by collection (tuned for Chris's usage patterns):
  messages / context        → 30 days   (recent convos matter most)
  experience                → 180 days  (learnings, errors)
  semantic_memory           → 365 days  (preferences are durable)
  canonical / knowledge     → ∞         (timeless — no decay)
  notes / tasks / calendar  → 90 days   (personal data, medium decay)

Usage:
    from time_decay import time_decay_multiplier
    mult = time_decay_multiplier(created_at="2026-01-01T00:00:00Z", collection="messages")
    final_score = base_score * mult
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

# collection → half-life in days (0 = no decay, ∞ effectively)
HALF_LIFE_DAYS: dict[str, int] = {
    "personal": 90,         # notes, calendar, tasks, messages (unified)
    "messages": 30,         # legacy
    "context": 30,
    "experience": 180,
    "notes": 90,            # legacy
    "tasks": 90,            # legacy
    "calendar": 90,         # legacy
    "semantic_memory": 365,
    "graph": 0,             # entity graph results — no decay
    "obsidian": 0,          # obsidian vault is reference material, no decay
    "knowledge": 0,         # configs, agent files — no decay
    "canonical": 0,         # authoritative truth — no decay
    "distilled": 0,         # summarized truth — no decay
    "patterns": 90,         # screen time daily patterns
}

# Never decay a result below this (keeps very old high-trust content findable).
DECAY_FLOOR = 0.25


def _parse_timestamp(raw: Any) -> datetime | None:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if not isinstance(raw, str):
        return None
    try:
        # Accept trailing Z and various ISO formats.
        txt = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def time_decay_multiplier(
    created_at: Any,
    collection: str,
    now: datetime | None = None,
) -> float:
    """Return a multiplier in [DECAY_FLOOR, 1.0] for a result's freshness."""
    half_life = HALF_LIFE_DAYS.get(collection, 0)
    if half_life <= 0:
        return 1.0  # no decay for this collection

    dt = _parse_timestamp(created_at)
    if dt is None:
        return 1.0  # unknown age → don't penalize

    now = now or datetime.now(timezone.utc)
    age_seconds = (now - dt).total_seconds()
    if age_seconds <= 0:
        return 1.0
    age_days = age_seconds / 86400.0

    mult = 0.5 ** (age_days / half_life)
    return max(DECAY_FLOOR, mult)


def apply_to_result(result: dict, debug: bool = False) -> dict:
    """Apply time decay to a single result in place and return it.

    Looks for `collection` at the top level and `created_at` either at top
    level or nested under `metadata`. Multiplies `score` by the decay factor.
    """
    collection = result.get("collection") or (result.get("metadata") or {}).get("collection") or ""
    created_at = (
        result.get("created_at")
        or (result.get("metadata") or {}).get("created_at")
        or (result.get("metadata") or {}).get("updated_at")
    )
    mult = time_decay_multiplier(created_at, collection)

    # Temporal validity: if valid_to is in the past, this fact is expired
    valid_to = result.get("valid_to") or (result.get("metadata") or {}).get("valid_to")
    if valid_to:
        vt = _parse_timestamp(valid_to)
        if vt and vt < datetime.now(timezone.utc):
            mult *= 0.3  # penalize expired facts but keep them retrievable

    base = float(result.get("score", 0))
    result["score"] = round(base * mult, 2)
    if debug:
        result.setdefault("_debug", {}).update({
            "decay_mult": round(mult, 3),
            "decay_base": base,
        })
    return result


def apply_to_results(results: list[dict], debug: bool = False) -> list[dict]:
    for r in results:
        apply_to_result(r, debug=debug)
    return results


if __name__ == "__main__":
    # Smoke test
    cases = [
        ("2026-04-07T00:00:00Z", "messages"),           # today → 1.0
        ("2026-03-08T00:00:00Z", "messages"),           # 30d old → 0.5
        ("2025-10-07T00:00:00Z", "messages"),           # 180d old → floor
        ("2025-04-07T00:00:00Z", "semantic_memory"),    # 365d old → 0.5
        ("2020-01-01T00:00:00Z", "canonical"),          # ancient → 1.0 (no decay)
        ("2026-04-07T00:00:00Z", "unknown_collection"), # today + unknown → 1.0
    ]
    for ts, col in cases:
        mult = time_decay_multiplier(ts, col)
        print(f"  [{col:18}] {ts} → {mult:.3f}")
