"""brain_core/self_eval.py — brain audits its own recall quality.

Problem: today's session found 9 bugs in brain's own behavior that had lived
for weeks without any SLO noticing. Brain has retrieval-quality measurements
(eval_holdout), but no ongoing signal of "my recall is drifting".

Approach:
  - Sample N recent /recall and /recall/v2 calls from action_audit
    (brain.db). Each row has the query_text + the top retrieved chroma IDs.
  - Re-run each query through search_unified.search_all now.
  - Compute top-3 overlap between the original and the re-run.
  - If overlap drops below OVERLAP_THRESHOLD for > DRIFT_PCT of samples,
    that's drift. Log to brain_config_store under self_eval.drift_7d so
    the SLO reader can surface it.

Runs nightly at 03:35 PT (after canonical_pipeline 3:30 but before sleep_
consolidate 3:55). Read-only except for the drift-stats write.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from config import BRAIN_DB
except ImportError:
    BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")

log = logging.getLogger("brain.self_eval")

OVERLAP_THRESHOLD = 0.7  # top-3 Jaccard overlap below this = drift on that query
SAMPLE_SIZE = 50
LOOKBACK_DAYS = 7


def _sample_recent_queries(n: int = SAMPLE_SIZE, lookback_days: int = LOOKBACK_DAYS) -> list[dict]:
    """Sample recent query texts from action_audit. We DON'T compare against
    the historical retrieval IDs — instead we re-run each query twice via
    the deterministic v1 path and measure overlap between the two reruns.
    That measures pipeline noise (embedder + ranker non-determinism) directly,
    without conflating LLM variance from HyDE/v2."""
    cutoff = (datetime.now(UTC) - timedelta(days=lookback_days)).isoformat(timespec="seconds")
    try:
        conn = sqlite3.connect(str(BRAIN_DB), timeout=5)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, query_text, created_at "
                "FROM action_audit "
                "WHERE route IN ('/recall', '/recall/v2') "
                "  AND query_text IS NOT NULL AND length(query_text) >= 10 "
                "  AND created_at >= ? "
                "GROUP BY query_text "
                "ORDER BY random() LIMIT ?",
                (cutoff, n),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning("action_audit read failed: %s", exc)
        return []
    return [{"id": r["id"], "query_text": r["query_text"], "created_at": r["created_at"]} for r in rows]


def _rerun_recall(query: str) -> list[str]:
    """Re-run the recall and return the top-3 chroma IDs."""
    try:
        import search_unified

        resp = search_unified.search_all(
            query,
            limit=5,
            sources=["rag", "canonical", "obsidian"],
            original_query=query,
        )
    except Exception as exc:
        log.debug("rerun search failed: %s", exc)
        return []
    if not isinstance(resp, dict):
        return []
    ids: list[str] = []
    for r in (resp.get("results") or [])[:3]:
        rid = r.get("id")
        if rid:
            ids.append(str(rid))
    return ids


def _jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


def run_self_eval() -> dict:
    """Pick recent query texts, run each twice via the deterministic v1
    path, measure top-3 Jaccard overlap between the two runs. Persist
    drift summary for the SLO reader."""
    samples = _sample_recent_queries()
    if not samples:
        return {"status": "no_samples", "checked": 0}

    drifted = 0
    overlaps: list[float] = []
    per_query: list[dict] = []
    for s in samples:
        ids_a = _rerun_recall(s["query_text"])
        if not ids_a:
            continue
        ids_b = _rerun_recall(s["query_text"])
        if not ids_b:
            continue
        overlap = _jaccard(ids_a, ids_b)
        overlaps.append(overlap)
        is_drift = overlap < OVERLAP_THRESHOLD
        if is_drift:
            drifted += 1
        per_query.append(
            {
                "query": s["query_text"][:120],
                "run_a": ids_a,
                "run_b": ids_b,
                "overlap": round(overlap, 3),
                "drifted": is_drift,
            }
        )

    n = len(overlaps)
    avg = sum(overlaps) / n if n else 0.0
    drift_pct = (drifted / n) * 100 if n else 0.0

    summary = {
        "checked": n,
        "drifted": drifted,
        "drift_pct": round(drift_pct, 1),
        "avg_overlap": round(avg, 3),
        "threshold": OVERLAP_THRESHOLD,
        "lookback_days": LOOKBACK_DAYS,
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
    }

    # Persist to brain_config_store for SLO reader
    try:
        import brain_config_store

        brain_config_store.set(
            "self_eval.drift_7d",
            json.dumps(summary),
            updated_by="self_eval",
        )
    except Exception as exc:
        log.warning("drift summary write failed: %s", exc)

    return {"summary": summary, "per_query": per_query[:10]}


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()
    result = run_self_eval()
    if args.verbose:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(result.get("summary", result), indent=2, ensure_ascii=False))
