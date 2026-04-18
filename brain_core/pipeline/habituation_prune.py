#!/Users/chrischo/server/brain/.venv/bin/python3
"""brain_core/pipeline/habituation_prune.py — attention_queue habituation sweep.

Biology: synaptic habituation — repeated exposure without reinforcement causes
a stimulus to drop out of the attention bottleneck. Our `attention_queue` table
tracks `shown_count`: how many times an atom has surfaced in /recall results.
Past a threshold, the atom is clearly not novel to Chris and continuing to
show it wastes slots for newer signal.

Action: delete rows where shown_count exceeds HABITUATION_THRESHOLD.

Runs nightly (3:20am PT) after the 3:15 sleep_consolidate so habituated
co-activation edges are captured before we drop the items.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from config import BRAIN_DB
except ImportError:
    BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")

HABITUATION_THRESHOLD = 300


def run(threshold: int = HABITUATION_THRESHOLD) -> dict:
    conn = sqlite3.connect(str(BRAIN_DB))
    try:
        total = conn.execute("SELECT COUNT(*) FROM attention_queue").fetchone()[0]
        stale_rows = conn.execute(
            "SELECT id, shown_count FROM attention_queue WHERE shown_count >= ?",
            (threshold,),
        ).fetchall()
        conn.execute(
            "DELETE FROM attention_queue WHERE shown_count >= ?",
            (threshold,),
        )
        conn.commit()
        remaining = conn.execute("SELECT COUNT(*) FROM attention_queue").fetchone()[0]
    finally:
        conn.close()
    return {
        "status": "ok",
        "total_before": total,
        "removed": len(stale_rows),
        "remaining": remaining,
        "threshold": threshold,
        "removed_ids": [r[0] for r in stale_rows[:20]],
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
    }


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--threshold", type=int, default=HABITUATION_THRESHOLD)
    args = p.parse_args()
    print(json.dumps(run(threshold=args.threshold), ensure_ascii=False))
