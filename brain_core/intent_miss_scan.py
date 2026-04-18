"""brain_core/intent_miss_scan.py — scheduled job: detect active_recall misses.

Runs nightly via scheduler.py::JOB_SCHEDULE. Scans action_audit for sessions
where turn N+1 (or later) contains correction phrases like "왜 브레인 안써",
meaning the previous /recall/active call missed what Chris actually wanted.
Writes each detected miss to eval_proposals as a route-learning candidate for
the weekly autonomy_proposer cluster pass.

No LLM. Pure SQL + regex. Runs in <100 ms typically.

Idempotent: re-running over the same window will see the same misses but won't
create duplicate eval_proposals rows because the fingerprint includes the
prompt text hash.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

try:
    from config import AUTONOMY_DB, BRAIN_LOGS_DIR
except ImportError:
    BRAIN_LOGS_DIR = Path("/Users/chrischo/server/brain/logs")
    AUTONOMY_DB = BRAIN_LOGS_DIR / "autonomy.db"

BRAIN_DB = BRAIN_LOGS_DIR / "brain.db"

log = logging.getLogger("brain.intent_miss_scan")

CORRECTION_PATTERNS = [
    r"왜\s*브레인\s*안\s*써",
    r"왜.*안\s*썼",
    r"내가\s*알려준",
    r"기억.*안\s*나",
    r"brain.*안\s*써",
    r"should\s+have\s+used\s+brain",
    r"did\s+not\s+use\s+brain",
    r"you\s+didn.t\s+(?:check|use|query)\s+brain",
    r"you\s+ignored",
    r"did\s+you\s+(?:even\s+)?(?:check|query)",
]
CORRECTION_REGEX = re.compile("|".join(CORRECTION_PATTERNS), re.IGNORECASE)


def _fingerprint(prev_prompt: str, correction_prompt: str) -> str:
    key = f"intent_miss:{prev_prompt[:300]}::{correction_prompt[:300]}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def run(since_days: int = 7) -> dict:
    """Scan /recall/active rows from the last N days for correction follow-ups.

    Returns {"scanned": N, "misses": N, "proposed": N} — for scheduler logging.
    """
    cutoff = (datetime.now(UTC) - timedelta(days=since_days)).isoformat()

    # Read turns from brain.db::action_audit
    try:
        with sqlite3.connect(str(BRAIN_DB), timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, created_at, query_text, session_id "
                "FROM action_audit "
                "WHERE route='/recall/active' AND created_at >= ? "
                "ORDER BY session_id, created_at ASC",
                (cutoff,),
            ).fetchall()
    except sqlite3.Error as e:
        log.warning("action_audit read failed: %s", e)
        return {"scanned": 0, "misses": 0, "proposed": 0, "error": str(e)[:200]}

    scanned = len(rows)
    if scanned == 0:
        return {"scanned": 0, "misses": 0, "proposed": 0}

    # Group by session
    by_session: dict[str, list] = {}
    for r in rows:
        by_session.setdefault(r["session_id"] or "unknown", []).append(r)

    misses: list[dict] = []
    for sid, turns in by_session.items():
        for i in range(1, len(turns)):
            prev = turns[i - 1]
            cur = turns[i]
            cur_text = cur["query_text"] or ""
            if CORRECTION_REGEX.search(cur_text):
                misses.append(
                    {
                        "session_id": sid,
                        "prev_ts": prev["created_at"],
                        "prev_prompt": prev["query_text"] or "",
                        "correction_ts": cur["created_at"],
                        "correction_prompt": cur_text,
                    }
                )

    # Write each miss to eval_proposals (dedup by fingerprint)
    proposed = 0
    if misses:
        try:
            with sqlite3.connect(str(AUTONOMY_DB), timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                # Ensure the category='intent_route_candidate' entries are
                # idempotent — use INSERT OR IGNORE keyed on id.
                for m in misses:
                    fp = _fingerprint(m["prev_prompt"], m["correction_prompt"])
                    pid = f"imiss_{fp}"
                    existing = conn.execute("SELECT id FROM eval_proposals WHERE id = ?", (pid,)).fetchone()
                    if existing:
                        continue
                    query = m["prev_prompt"][:1000]
                    expected = json.dumps(
                        {
                            "correction_signal": m["correction_prompt"][:500],
                            "session_id": m["session_id"],
                            "prev_ts": m["prev_ts"],
                            "correction_ts": m["correction_ts"],
                        }
                    )
                    conn.execute(
                        "INSERT INTO eval_proposals "
                        "(id, query, expected, expected_sources, source_event, "
                        " status, confidence, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            pid,
                            query,
                            expected,
                            "[]",
                            "intent_miss_scan",
                            "candidate",
                            0.7,
                            datetime.now(UTC).isoformat(timespec="seconds"),
                        ),
                    )
                    proposed += 1
                conn.commit()
        except sqlite3.Error as e:
            log.warning("eval_proposals write failed: %s", e)
            return {
                "scanned": scanned,
                "misses": len(misses),
                "proposed": proposed,
                "error": str(e)[:200],
            }

    log.info(
        "intent_miss_scan: scanned=%d misses=%d proposed=%d (since %s)",
        scanned,
        len(misses),
        proposed,
        cutoff[:10],
    )
    return {"scanned": scanned, "misses": len(misses), "proposed": proposed}


if __name__ == "__main__":
    result = run()
    print(json.dumps(result))
