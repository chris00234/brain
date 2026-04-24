"""Persistence and reports for active-recall judgment decisions.

The hot-path judgment layer is deterministic and cheap. This module records its
decisions beside action_audit so later labelers can learn whether prompt-level
gating was useful or noisy without adding another daemon or LLM call.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import BRAIN_DB

log = logging.getLogger("brain.judgment_feedback")


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS active_recall_judgments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_audit_id INTEGER,
            session_id TEXT,
            actor TEXT NOT NULL DEFAULT 'unknown',
            prompt_intent TEXT NOT NULL,
            needs_memory INTEGER NOT NULL,
            allow_semantic INTEGER NOT NULL,
            allow_proactive INTEGER NOT NULL,
            max_blocks INTEGER NOT NULL,
            max_tokens INTEGER NOT NULL,
            min_semantic_score REAL NOT NULL,
            block_count INTEGER NOT NULL,
            semantic_count INTEGER NOT NULL,
            suppressed_json TEXT NOT NULL DEFAULT '{}',
            latency_ms INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_arj_audit
          ON active_recall_judgments(action_audit_id);
        CREATE INDEX IF NOT EXISTS idx_arj_intent_ts
          ON active_recall_judgments(prompt_intent, created_at);
        CREATE INDEX IF NOT EXISTS idx_arj_actor_ts
          ON active_recall_judgments(actor, created_at);
        """
    )
    conn.commit()


def record(
    *,
    action_audit_id: int | None,
    session_id: str | None,
    actor: str | None,
    judgment: object | None,
    arbitration: object | None,
    block_count: int,
    semantic_count: int,
    latency_ms: int,
    db_path: Path | None = None,
) -> None:
    """Best-effort write of one active-recall judgment decision."""

    if judgment is None or not hasattr(judgment, "to_dict"):
        return
    data = judgment.to_dict()
    suppressed: dict = {}
    if arbitration is not None and hasattr(arbitration, "to_quality_dict"):
        suppressed = arbitration.to_quality_dict().get("suppressed") or {}

    try:
        conn = sqlite3.connect(str(db_path or BRAIN_DB), timeout=5)
    except Exception:
        return
    try:
        _ensure_table(conn)
        conn.execute(
            "INSERT INTO active_recall_judgments "
            "(action_audit_id, session_id, actor, prompt_intent, needs_memory, "
            " allow_semantic, allow_proactive, max_blocks, max_tokens, "
            " min_semantic_score, block_count, semantic_count, suppressed_json, "
            " latency_ms, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                action_audit_id,
                session_id,
                actor or "unknown",
                str(data.get("intent") or "unknown"),
                1 if data.get("needs_memory") else 0,
                1 if data.get("allow_semantic") else 0,
                1 if data.get("allow_proactive") else 0,
                int(data.get("max_blocks") or 0),
                int(data.get("max_tokens") or 0),
                float(data.get("min_semantic_score") or 0.0),
                int(block_count),
                int(semantic_count),
                json.dumps(suppressed, sort_keys=True),
                int(latency_ms),
                datetime.now(UTC).isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
    except sqlite3.Error as exc:
        log.debug("active_recall judgment record failed: %s", exc)
    finally:
        conn.close()


def report(hours: int = 24, db_path: Path | None = None) -> dict:
    """Return lightweight judgment-gate telemetry for the trailing window."""

    conn = sqlite3.connect(str(db_path or BRAIN_DB))
    conn.row_factory = sqlite3.Row
    try:
        _ensure_table(conn)
        rows = conn.execute(
            """
            SELECT prompt_intent,
                   COUNT(*) AS calls,
                   SUM(CASE WHEN needs_memory = 0 THEN 1 ELSE 0 END) AS suppressed_prompts,
                   AVG(block_count) AS avg_blocks,
                   AVG(semantic_count) AS avg_semantic_blocks,
                   AVG(latency_ms) AS avg_latency_ms
            FROM active_recall_judgments
            WHERE created_at > datetime('now', ? || ' hours')
            GROUP BY prompt_intent
            ORDER BY calls DESC
            """,
            (f"-{int(hours)}",),
        ).fetchall()
        suppressed_rows = conn.execute(
            """
            SELECT suppressed_json
            FROM active_recall_judgments
            WHERE created_at > datetime('now', ? || ' hours')
            """,
            (f"-{int(hours)}",),
        ).fetchall()
    finally:
        conn.close()

    suppressed: dict[str, int] = {}
    for row in suppressed_rows:
        try:
            parsed = json.loads(row["suppressed_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        for key, value in parsed.items():
            try:
                suppressed[str(key)] = suppressed.get(str(key), 0) + int(value)
            except (TypeError, ValueError):
                continue

    by_intent = [
        {
            "intent": row["prompt_intent"],
            "calls": int(row["calls"] or 0),
            "suppressed_prompts": int(row["suppressed_prompts"] or 0),
            "avg_blocks": round(float(row["avg_blocks"] or 0.0), 2),
            "avg_semantic_blocks": round(float(row["avg_semantic_blocks"] or 0.0), 2),
            "avg_latency_ms": round(float(row["avg_latency_ms"] or 0.0), 1),
        }
        for row in rows
    ]
    return {"window_hours": hours, "by_intent": by_intent, "suppressed": dict(sorted(suppressed.items()))}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=24)
    args = parser.parse_args()
    sys.stdout.write(json.dumps(report(hours=args.hours), indent=2) + "\n")
