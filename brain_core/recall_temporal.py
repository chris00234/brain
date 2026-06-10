"""Temporal filtering helpers for recall endpoints.

This module keeps Python-side temporal post-filtering out of the FastAPI route
body while preserving the exact behavior required by ChromaDB string-date
limitations.
"""

from __future__ import annotations

from datetime import datetime

import temporal


def _apply_temporal_filter_inplace(
    payloads: list[dict],
    start_dt: datetime | None,
    end_dt: datetime | None,
) -> None:
    """Apply Python-side temporal filter to each payload's results in place.

    ChromaDB 1.4.1 can't range-filter string datetime fields, so the temporal
    bounds get applied post-search. No-op if both bounds are None.

    Mutates each payload's `results` list in place; returns None.
    """
    if not (start_dt or end_dt):
        return
    for p in payloads:
        if isinstance(p, dict) and p.get("results"):
            p["results"] = temporal.filter_by_created_at(p["results"], start_dt, end_dt)
