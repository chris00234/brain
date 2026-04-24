"""brain_core/retrieval_inhibition.py — Bjork (1994) retrieval-induced inhibition.

When two atoms compete for the same query cue and one consistently wins,
the loser should have its confidence slightly decremented. This prevents
the rich-get-richer spiral where frequently-retrieved atoms dominate future
retrieval regardless of whether they're actually the right answer.

Two surfaces:
  1. log_competition(winner_id, loser_ids, query)  — called from recall_v2
     after the top-K is finalized. Records per-cue (winner, loser, n) rows.
  2. run_inhibition_pass()  — nightly job. For each (winner, loser, n>=3)
     pair seen in the last 14 days, writes a small negative atom_evidence
     row against the loser, decaying its confidence via the Bayesian
     ledger. Then vacuums rows older than 60 days.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from atoms_store import BRAIN_DB, _conn
except ImportError:
    BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")
    _conn = None  # type: ignore


# Minimum observations before we start penalizing. Below this, the signal
# is too sparse to distinguish bad ranking from a single bad query.
MIN_OBSERVATIONS_FOR_INHIBITION = 3
INHIBITION_LOOKBACK_DAYS = 14
INHIBITION_WEIGHT = -0.02  # small nudge in logit space; compounds over repeats
VACUUM_AFTER_DAYS = 60

_CUE_WORD_RE = re.compile(r"\w{2,}")


def _query_cue_hash(query: str) -> str:
    """Coarse cluster hash: lowercase + sort tokens + md5 prefix.

    Different phrasings of the same question map to the same cue bucket,
    so competition observations accumulate rather than fragmenting across
    near-synonymous queries.
    """
    if not query:
        return ""
    tokens = sorted(set(_CUE_WORD_RE.findall(query.lower())))
    joined = " ".join(tokens[:8])  # cap at 8 most-informative tokens
    return hashlib.md5(joined.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


def log_competition(winner_atom_id: str, loser_atom_ids: list[str], query: str) -> int:
    """Record that `winner` beat `losers` on the cue cluster for `query`.

    Best-effort — never raises. Returns number of rows upserted.
    """
    if not winner_atom_id or not loser_atom_ids or _conn is None:
        return 0
    cue = _query_cue_hash(query)
    if not cue:
        return 0
    now_iso = datetime.now(UTC).isoformat(timespec="seconds")
    rows_written = 0
    try:
        with _conn() as conn:
            for loser_id in loser_atom_ids:
                if not loser_id or loser_id == winner_atom_id:
                    continue
                try:
                    conn.execute(
                        "INSERT INTO retrieval_competition "
                        "(winner_atom_id, loser_atom_id, query_cue_hash, n_observations, last_seen_at) "
                        "VALUES (?, ?, ?, 1, ?) "
                        "ON CONFLICT (winner_atom_id, loser_atom_id, query_cue_hash) DO UPDATE SET "
                        "  n_observations = n_observations + 1, "
                        "  last_seen_at = excluded.last_seen_at",
                        (winner_atom_id, loser_id, cue, now_iso),
                    )
                    rows_written += 1
                except sqlite3.Error:
                    continue
            conn.commit()
    except sqlite3.Error:
        return rows_written
    return rows_written


def run_inhibition_pass() -> dict:
    """Nightly inhibition job — apply confidence decrements to consistent losers.

    Called from brain scheduler. Returns summary dict.
    """
    if _conn is None:
        return {"status": "skip", "reason": "atoms_store unavailable"}
    cutoff_iso = (datetime.now(UTC) - timedelta(days=INHIBITION_LOOKBACK_DAYS)).isoformat(timespec="seconds")
    vacuum_iso = (datetime.now(UTC) - timedelta(days=VACUUM_AFTER_DAYS)).isoformat(timespec="seconds")
    losers_inhibited = 0
    evidence_rows = 0
    try:
        with _conn() as conn:
            # Aggregate by loser atom: total observations, distinct winners.
            rows = conn.execute(
                "SELECT loser_atom_id, SUM(n_observations) AS total_losses, "
                "       COUNT(DISTINCT winner_atom_id) AS distinct_winners "
                "FROM retrieval_competition "
                "WHERE last_seen_at >= ? "
                "GROUP BY loser_atom_id "
                "HAVING total_losses >= ?",
                (cutoff_iso, MIN_OBSERVATIONS_FOR_INHIBITION),
            ).fetchall()
            now_iso = datetime.now(UTC).isoformat(timespec="seconds")
            for row in rows:
                loser = row["loser_atom_id"]
                total = int(row["total_losses"])
                # Cap weight so a single runaway competition can't crush
                # an atom — decrement proportional to log(n) with a floor.
                import math as _math

                weight = max(INHIBITION_WEIGHT * _math.log2(total + 1), -0.15)
                try:
                    conn.execute(
                        "INSERT INTO atom_evidence "
                        "(atom_id, event_type, weight, evidence_ref, cluster_size, created_at) "
                        "VALUES (?, 'retrieval_inhibition', ?, ?, ?, ?)",
                        (loser, weight, f"n={total}", int(row["distinct_winners"]), now_iso),
                    )
                    evidence_rows += 1
                    losers_inhibited += 1
                except sqlite3.Error:
                    continue
            # Vacuum old rows so the table stays bounded.
            cur = conn.execute(
                "DELETE FROM retrieval_competition WHERE last_seen_at < ?",
                (vacuum_iso,),
            )
            vacuumed = cur.rowcount
            conn.commit()
    except sqlite3.Error as e:
        return {"status": "error", "reason": str(e)[:200]}
    return {
        "status": "ok",
        "losers_inhibited": losers_inhibited,
        "evidence_rows": evidence_rows,
        "vacuumed": vacuumed,
        "lookback_days": INHIBITION_LOOKBACK_DAYS,
    }


if __name__ == "__main__":
    import json

    print(json.dumps(run_inhibition_pass(), indent=2))
