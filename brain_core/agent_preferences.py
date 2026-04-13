"""brain_core/agent_preferences.py — Per-agent source preference weights.

Each agent learns which sources give them the most useful results via
feedback signals. Weights are stored in SQLite and updated weekly by
feedback_aggregator.
"""
from __future__ import annotations

import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timezone

log = logging.getLogger("brain.agent_prefs")

PREFS_DB = Path("/Users/chrischo/server/brain/logs/autonomy.db")

_schema_initialized = False


def _conn():
    PREFS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(PREFS_DB))
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure_schema():
    global _schema_initialized
    if _schema_initialized:
        return
    conn = _conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_source_prefs (
                agent TEXT NOT NULL,
                source TEXT NOT NULL,
                weight REAL NOT NULL DEFAULT 1.0,
                useful_count INTEGER DEFAULT 0,
                total_count INTEGER DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (agent, source)
            )
        """)
        conn.commit()
        _schema_initialized = True
    finally:
        conn.close()


def get_agent_weights(agent: str) -> dict[str, float]:
    """Returns {source: weight} mapping for the agent. Defaults to 1.0 if not set."""
    ensure_schema()
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT source, weight FROM agent_source_prefs WHERE agent = ?",
            (agent,)
        ).fetchall()
        return {source: float(weight) for source, weight in rows}
    finally:
        conn.close()


def record_feedback(agent: str, source: str, useful: bool) -> None:
    """Record one feedback event for an (agent, source) pair."""
    ensure_schema()
    conn = _conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        # Upsert pattern
        conn.execute("""
            INSERT INTO agent_source_prefs (agent, source, weight, useful_count, total_count, updated_at)
            VALUES (?, ?, 1.0, ?, 1, ?)
            ON CONFLICT(agent, source) DO UPDATE SET
                useful_count = useful_count + ?,
                total_count = total_count + 1,
                updated_at = ?
        """, (agent, source, 1 if useful else 0, now, 1 if useful else 0, now))
        conn.commit()
    finally:
        conn.close()


def recompute_weights() -> dict:
    """Recompute all weights from useful_count/total_count ratios.

    Called weekly by feedback_aggregator. Weights normalized so average is 1.0.
    """
    ensure_schema()
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT agent, source, useful_count, total_count FROM agent_source_prefs WHERE total_count >= 5"
        ).fetchall()

        updated = 0
        for agent, source, useful, total in rows:
            ratio = useful / total if total > 0 else 0.5
            # Map 0-1 ratio to 0.5-1.5 weight (prevents hard-exclusion of any source)
            weight = 0.5 + ratio
            conn.execute(
                "UPDATE agent_source_prefs SET weight = ? WHERE agent = ? AND source = ?",
                (weight, agent, source)
            )
            updated += 1

        conn.commit()
        return {"updated": updated}
    finally:
        conn.close()


if __name__ == "__main__":
    import json
    print(json.dumps(recompute_weights(), indent=2))
