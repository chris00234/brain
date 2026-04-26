"""brain_core/recall_outcome_labeler.py — auto-label pending recall outcomes.

action_audit records every /recall/v2 + /recall/active call with outcome=NULL.
By 2026-04-23 the 7-day backlog sat at ~24k rows, 100% unlabeled — infrastructure
present, no labeler ever built. Without labels the self-learning loop has no
signal from what's actually the hottest surface in the system.

This module ships the minimum viable labeler: if the same session issues a
semantically similar recall within 120 seconds of a pending one, mark the
earlier row outcome='restated'. Restated is a weak "wrong" signal — the caller
didn't find what they needed and re-asked. Stronger signals (contradiction
propagation, explicit /recall/feedback ack) layer on top in later passes.

Cosine similarity uses the same embedder as recall. Pairs are filtered to the
same session_id so concurrent unrelated recalls don't cross-contaminate.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from indexer import get_embedding

from config import BRAIN_DB

log = logging.getLogger("brain.recall_outcome_labeler")

RESTATE_WINDOW_SEC = 120
RESTATE_COSINE_MIN = 0.85
RECALL_ROUTES = ("/recall/v2", "/recall/active")


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def label_restated_recalls(hours: int = 24, dry_run: bool = False) -> dict:
    """Scan pending recalls; mark 'restated' when same-session retry within window.

    Returns a counters dict: {scanned, restated, skipped_no_query, skipped_embed}.
    """
    cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat(timespec="seconds")
    conn = sqlite3.connect(str(BRAIN_DB))
    conn.row_factory = sqlite3.Row
    counters = {"scanned": 0, "restated": 0, "skipped_no_query": 0, "skipped_embed": 0}
    try:
        route_placeholders = ",".join("?" * len(RECALL_ROUTES))
        rows = conn.execute(
            f"SELECT id, query_text, session_id, created_at "  # noqa: S608 — fixed placeholder count
            f"FROM action_audit "
            f"WHERE outcome IS NULL "
            f"  AND route IN ({route_placeholders}) "
            f"  AND session_id IS NOT NULL "
            f"  AND created_at > ? "
            f"ORDER BY session_id, created_at",
            (*RECALL_ROUTES, cutoff),
        ).fetchall()

        # Group by session to keep the pair scan O(n) per session
        by_session: dict[str, list[sqlite3.Row]] = {}
        for r in rows:
            by_session.setdefault(r["session_id"], []).append(r)

        embed_cache: dict[str, list[float]] = {}

        for session_rows in by_session.values():
            if len(session_rows) < 2:
                continue
            for i, earlier in enumerate(session_rows[:-1]):
                counters["scanned"] += 1
                q1 = earlier["query_text"]
                if not q1:
                    counters["skipped_no_query"] += 1
                    continue
                t1 = datetime.fromisoformat(earlier["created_at"].replace("Z", "+00:00"))
                # Scan forward until the window closes
                for later in session_rows[i + 1 :]:
                    t2 = datetime.fromisoformat(later["created_at"].replace("Z", "+00:00"))
                    delta = (t2 - t1).total_seconds()
                    if delta > RESTATE_WINDOW_SEC:
                        break
                    q2 = later["query_text"]
                    if not q2:
                        continue
                    # Embed lazily, cache per-row
                    if earlier["id"] not in embed_cache:
                        emb1 = get_embedding(q1[:1000], prefix="query")
                        if not emb1:
                            counters["skipped_embed"] += 1
                            break
                        embed_cache[earlier["id"]] = emb1
                    if later["id"] not in embed_cache:
                        emb2 = get_embedding(q2[:1000], prefix="query")
                        if not emb2:
                            counters["skipped_embed"] += 1
                            continue
                        embed_cache[later["id"]] = emb2
                    sim = _cosine(embed_cache[earlier["id"]], embed_cache[later["id"]])
                    if sim >= RESTATE_COSINE_MIN:
                        counters["restated"] += 1
                        if not dry_run:
                            conn.execute(
                                "UPDATE action_audit SET outcome = ?, outcome_reason = ?, "
                                "resolved_at = ? WHERE id = ?",
                                (
                                    "restated",
                                    json.dumps(
                                        {
                                            "next_id": later["id"],
                                            "delta_sec": round(delta, 1),
                                            "cosine": round(sim, 3),
                                        }
                                    ),
                                    datetime.now(UTC).isoformat(timespec="seconds"),
                                    earlier["id"],
                                ),
                            )
                        break  # one restate label per earlier row is enough

        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    log.info("recall_outcome_labeler: %s", counters)
    return counters


def run(dry_run: bool = False, hours: int = 24) -> dict:
    """Entry point for scheduler."""
    return label_restated_recalls(hours=hours, dry_run=dry_run)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--hours", type=int, default=24)
    args = parser.parse_args()
    result = run(dry_run=args.dry_run, hours=args.hours)
    print(json.dumps(result, indent=2))
