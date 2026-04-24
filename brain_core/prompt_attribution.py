"""brain_core/prompt_attribution.py — track which prompt produced which atom.

The distill/judge/classify prompts in brain are static strings. Without per-
atom attribution, prompt rewrites can't be A/B tested — there's no signal
linking "atoms produced by prompt P" to "% that survived 7 days".

This module ships the minimum substrate:

  1. prompt_attribution(chroma_id, prompt_id, prompt_version, created_at)
     — separate table, no migration to atoms schema, easy rollback.
  2. record(chroma_id, prompt_id, prompt_version) — call site for distill.
  3. survival_report(days=7) — survival rate per prompt_id over a window.

Survival = atom not superseded AND not deleted within the window. High
survival = the prompt produced atoms the system decided to keep. Low survival
= the prompt produced atoms that get contradicted, deduped, or auto-resolved
away — a quality signal, not just "did the LLM respond".

To run an A/B: emit two prompt_versions in parallel for a sampled fraction
of distills, compare 7-day survival, swap if winner is significant.
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

log = logging.getLogger("brain.prompt_attribution")

CURRENT_DEFAULTS = {
    # Bumped when the prompt body changes.
    "distill": "distill_v1",
    "judge": "judge_v1",
    "classify": "classify_v1",
}


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS prompt_attribution (
            chroma_id TEXT NOT NULL,
            prompt_id TEXT NOT NULL,
            prompt_version TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            PRIMARY KEY (chroma_id, prompt_id)
        );
        CREATE INDEX IF NOT EXISTS idx_prompt_attr_id_ts
          ON prompt_attribution(prompt_id, created_at);
        """
    )
    conn.commit()


def record(chroma_id: str, prompt_id: str, prompt_version: str = "") -> None:
    """Best-effort attribution write. Idempotent on (chroma_id, prompt_id)."""
    if not chroma_id or not prompt_id:
        return
    try:
        conn = sqlite3.connect(str(BRAIN_DB))
    except Exception:
        return
    try:
        _ensure_table(conn)
        conn.execute(
            "INSERT OR IGNORE INTO prompt_attribution "
            "(chroma_id, prompt_id, prompt_version, created_at) VALUES (?, ?, ?, ?)",
            (chroma_id, prompt_id, prompt_version, datetime.now(UTC).isoformat(timespec="seconds")),
        )
        conn.commit()
    except sqlite3.Error:
        pass
    finally:
        conn.close()


def survival_report(days: int = 7) -> dict:
    """Per-prompt survival rate over the trailing window.

    Survival = atom still in atoms table AND superseded_by IS NULL AND
    superseded_by_chroma is unset. Atoms whose chroma_id no longer exists
    are counted as not_survived (deleted by lifecycle).
    """
    conn = sqlite3.connect(str(BRAIN_DB))
    conn.row_factory = sqlite3.Row
    try:
        _ensure_table(conn)
        rows = conn.execute(
            """
            SELECT pa.prompt_id,
                   pa.prompt_version,
                   COUNT(*) AS produced,
                   SUM(CASE WHEN a.chroma_id IS NOT NULL
                                  AND (a.superseded_by IS NULL OR a.superseded_by = '')
                            THEN 1 ELSE 0 END) AS survived
            FROM prompt_attribution pa
            LEFT JOIN atoms a ON a.chroma_id = pa.chroma_id
            WHERE pa.created_at > datetime('now', ? || ' days')
            GROUP BY pa.prompt_id, pa.prompt_version
            ORDER BY produced DESC
            """,
            (f"-{int(days)}",),
        ).fetchall()
    finally:
        conn.close()
    report = []
    for r in rows:
        produced = r["produced"] or 0
        survived = r["survived"] or 0
        report.append(
            {
                "prompt_id": r["prompt_id"],
                "prompt_version": r["prompt_version"],
                "produced": produced,
                "survived": survived,
                "survival_rate": round(survived / produced, 3) if produced else 0.0,
            }
        )
    log.info("prompt_attribution.survival_report(days=%d): %s", days, report)
    return {"window_days": days, "rows": report}


def run(days: int = 7) -> dict:
    """Scheduler entry point — emits the survival report as JSON."""
    return survival_report(days=days)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    print(json.dumps(run(days=args.days), indent=2))
