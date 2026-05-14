"""Belief diff — what changed in Brain's understanding over a window.

Pure read. Surfaces three signals that aren't otherwise discoverable
without manually correlating SQLite tables:

  1. Newly canonical atoms      — promotions that joined the truth layer
  2. New supersession edges     — facts that the brain explicitly replaced
  3. Reversed / failed decisions — recommendations the brain walked back

All windows are wall-clock days from "now" (UTC). No mutation, no LLM.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from config import AUTONOMY_DB, BRAIN_DB

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 500


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _row_dicts(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row, strict=False)) for row in cursor.fetchall()]


def _newly_canonical(conn: sqlite3.Connection, since_days: int, limit: int) -> list[dict]:
    cur = conn.execute(
        """
        SELECT id, text, kind, tier, confidence, trust_score, created_at, updated_at
        FROM atoms
        WHERE canonical = 1
          AND updated_at > datetime('now', ?)
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (f"-{int(since_days)} days", int(limit)),
    )
    return _row_dicts(cur)


def _new_supersessions(conn: sqlite3.Connection, since_days: int, limit: int) -> list[dict]:
    cur = conn.execute(
        """
        SELECT
            sup.id          AS new_atom_id,
            sup.text        AS new_text,
            sup.tier        AS new_tier,
            sup.updated_at  AS edge_at,
            old.id          AS replaced_atom_id,
            old.text        AS replaced_text,
            old.tier        AS replaced_tier
        FROM atoms sup
        JOIN atoms old ON old.id = sup.supersedes
        WHERE sup.supersedes IS NOT NULL
          AND sup.updated_at > datetime('now', ?)
        ORDER BY sup.updated_at DESC
        LIMIT ?
        """,
        (f"-{int(since_days)} days", int(limit)),
    )
    return _row_dicts(cur)


def _reversed_decisions(since_days: int, limit: int) -> list[dict]:
    if not AUTONOMY_DB.exists():
        return []
    conn = sqlite3.connect(str(AUTONOMY_DB), timeout=5)
    try:
        cur = conn.execute(
            """
            SELECT id, created_at, resolved_at, actor, domain, source,
                   observation_subject, selected_option, outcome_status, review_status
            FROM decision_ledger
            WHERE (review_status = 'reversed' OR outcome_status = 'failed')
              AND COALESCE(resolved_at, created_at) > datetime('now', ?)
            ORDER BY COALESCE(resolved_at, created_at) DESC
            LIMIT ?
            """,
            (f"-{int(since_days)} days", int(limit)),
        )
        return _row_dicts(cur)
    finally:
        conn.close()


def _tier_growth(conn: sqlite3.Connection, since_days: int) -> dict[str, int]:
    cur = conn.execute(
        """
        SELECT tier, COUNT(*)
        FROM atoms
        WHERE created_at > datetime('now', ?)
        GROUP BY tier
        """,
        (f"-{int(since_days)} days",),
    )
    return {tier: int(count) for tier, count in cur.fetchall()}


def compute_diff(since_days: int = 7, limit: int = _DEFAULT_LIMIT) -> dict[str, Any]:
    """Return all change signals for the window. Single read pass per DB."""
    since_days = _clamp(since_days, 1, 90)
    limit = _clamp(limit, 1, _MAX_LIMIT)
    summary: dict[str, Any] = {
        "since_days": since_days,
        "limit": limit,
        "newly_canonical": [],
        "new_supersessions": [],
        "reversed_decisions": [],
        "tier_growth": {},
        "totals": {},
    }
    if BRAIN_DB.exists():
        conn = sqlite3.connect(str(BRAIN_DB), timeout=5)
        try:
            summary["newly_canonical"] = _newly_canonical(conn, since_days, limit)
            summary["new_supersessions"] = _new_supersessions(conn, since_days, limit)
            summary["tier_growth"] = _tier_growth(conn, since_days)
        finally:
            conn.close()
    summary["reversed_decisions"] = _reversed_decisions(since_days, limit)
    summary["totals"] = {
        "newly_canonical": len(summary["newly_canonical"]),
        "new_supersessions": len(summary["new_supersessions"]),
        "reversed_decisions": len(summary["reversed_decisions"]),
        "tier_growth_total": sum(summary["tier_growth"].values()),
    }
    return summary
