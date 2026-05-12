"""brain_core/valence.py — Emotional valence layer (biological: amygdala).

2026-04-17: Chris Priority #1 (learning Chris + pattern recognition).

Brain has no affective tagging — every memory is equally weighted regardless of
whether Chris loved it, corrected it, or was frustrated by it. Biological brain
uses the amygdala to stamp valence (good/bad/neutral) on episodic memories,
which then modulates retrieval priority AND consolidation strength.

This module adds a thin, additive valence layer via a separate SQLite table
(no schema change to atoms — zero regression risk on search_unified stable eval
94.9%). Valence feeds search_unified as a small multiplicative boost (like
preference recency) and can be queried independently for analytics.

Valence values:
  +1.0  strong positive (Chris explicitly praised, "완벽", "exactly")
  +0.5  mild positive (Chris accepted without correction)
   0.0  neutral (no signal)
  -0.5  mild negative (Chris corrected gently)
  -1.0  strong negative (Chris rejected, "아니야", "별로")

Valence accumulates: second positive event on same atom → avg((+0.5, +1.0)) = +0.75
rather than just overwriting. This smooths out noisy single events.

Write paths (future integration):
  - Session-end distillation tags corrections (-0.5) and praised patterns (+0.5)
  - Explicit POST /brain/valence/{atom_id} (Chris can manually tag)
  - Correction triggers from self-learning protocol (see CLAUDE.md)

Read paths:
  - search_unified applies small multiplicative boost (±15% max)
  - /brain/valence/top surfaces most-positive + most-negative
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

log = logging.getLogger("brain.valence")

try:
    from config import BRAIN_DB
except ImportError:
    BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")

# Boost bounds. Small so a miscalibrated valence can't torch stable eval.
MAX_BOOST = 0.15  # +15% score bump for +1.0 valence
MIN_BOOST = -0.10  # -10% score penalty for -1.0 valence (asymmetric — we trust
# Chris's corrections more strongly than positives).

# 2026-04-17 prod-review fix: schema idempotency flag. _ensure_schema was
# hot-path on get_valence_batch (called per /recall/v2) — per-call open+close
# of a connection for a no-op CREATE TABLE IF NOT EXISTS adds measurable
# latency to the recall SLO budget.
_schema_done = False


def _ensure_schema() -> None:
    global _schema_done
    if _schema_done:
        return
    conn = sqlite3.connect(str(BRAIN_DB))
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS atom_valence (
                atom_id       TEXT PRIMARY KEY,
                valence       REAL NOT NULL DEFAULT 0.0,    -- [-1.0, +1.0]
                event_count   INTEGER NOT NULL DEFAULT 0,   -- N events averaged
                last_updated  TEXT NOT NULL,
                last_reason   TEXT NOT NULL DEFAULT '',
                last_source   TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_atom_valence_val ON atom_valence(valence);
            """
        )
        conn.commit()
        _schema_done = True
    finally:
        conn.close()


from db import now_iso as _now_iso  # noqa: E402  — single-source UTC stamp helper


def record_valence(
    atom_id: str,
    delta: float,
    *,
    reason: str = "",
    source: str = "",
) -> dict:
    """Update atom valence with a new event. Averages in.

    delta is the event's signed contribution in [-1.0, +1.0]. It's averaged
    with the existing valence weighted by event_count so a noisy single event
    doesn't dominate long-established tags.
    """
    if not atom_id:
        return {"ok": False, "error": "empty_atom_id"}
    delta = max(-1.0, min(1.0, float(delta)))
    _ensure_schema()
    conn = sqlite3.connect(str(BRAIN_DB))
    try:
        # 2026-04-18: wrap SELECT + UPDATE in BEGIN IMMEDIATE so two concurrent
        # record_valence calls on the same atom_id (e.g. a session-end burst of
        # corrections) can't lose-update each other's running average. Matches
        # the pattern used in atoms_store.reinforce, mark_superseded, and
        # update_atom_confidence.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT valence, event_count FROM atom_valence WHERE atom_id = ?", (atom_id,)
        ).fetchone()
        if row:
            prev_val, prev_n = float(row[0]), int(row[1])
            new_n = prev_n + 1
            new_val = ((prev_val * prev_n) + delta) / new_n
            new_val = max(-1.0, min(1.0, new_val))
            conn.execute(
                "UPDATE atom_valence SET valence = ?, event_count = ?, "
                "last_updated = ?, last_reason = ?, last_source = ? "
                "WHERE atom_id = ?",
                (round(new_val, 4), new_n, _now_iso(), reason[:200], source[:100], atom_id),
            )
        else:
            conn.execute(
                "INSERT INTO atom_valence (atom_id, valence, event_count, "
                "last_updated, last_reason, last_source) VALUES (?, ?, 1, ?, ?, ?)",
                (atom_id, round(delta, 4), _now_iso(), reason[:200], source[:100]),
            )
            new_val = delta
            new_n = 1
        conn.commit()
        return {"ok": True, "atom_id": atom_id, "valence": round(new_val, 4), "event_count": new_n}
    finally:
        conn.close()


def get_valence(atom_id: str) -> float:
    """Fast read — returns 0.0 when no row exists."""
    _ensure_schema()
    conn = sqlite3.connect(str(BRAIN_DB))
    try:
        row = conn.execute("SELECT valence FROM atom_valence WHERE atom_id = ?", (atom_id,)).fetchone()
        return float(row[0]) if row else 0.0
    finally:
        conn.close()


def get_valence_batch(atom_ids: list[str]) -> dict[str, float]:
    """Batch read for search_unified — one SQL per /recall call, not per atom."""
    if not atom_ids:
        return {}
    _ensure_schema()
    conn = sqlite3.connect(str(BRAIN_DB))
    try:
        placeholders = ",".join("?" for _ in atom_ids)
        rows = conn.execute(
            f"SELECT atom_id, valence FROM atom_valence WHERE atom_id IN ({placeholders})",
            atom_ids,
        ).fetchall()
        return {r[0]: float(r[1]) for r in rows}
    finally:
        conn.close()


def valence_to_boost(valence: float) -> float:
    """Map [-1.0, +1.0] valence to a score multiplier delta in [MIN_BOOST, MAX_BOOST].

    Caller applies as: final_score = base_score * (1.0 + valence_to_boost(v))
    """
    if valence >= 0:
        return round(valence * MAX_BOOST, 4)
    return round(valence * abs(MIN_BOOST), 4)


def top_valence(limit: int = 20, direction: str = "both") -> list[dict]:
    """Return top-valence atoms for observability / debugging.

    direction: 'positive' | 'negative' | 'both'
    """
    _ensure_schema()
    conn = sqlite3.connect(str(BRAIN_DB))
    try:
        if direction == "positive":
            rows = conn.execute(
                "SELECT atom_id, valence, event_count, last_updated, last_reason "
                "FROM atom_valence WHERE valence > 0 ORDER BY valence DESC LIMIT ?",
                (limit,),
            ).fetchall()
        elif direction == "negative":
            rows = conn.execute(
                "SELECT atom_id, valence, event_count, last_updated, last_reason "
                "FROM atom_valence WHERE valence < 0 ORDER BY valence ASC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT atom_id, valence, event_count, last_updated, last_reason "
                "FROM atom_valence ORDER BY ABS(valence) DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "atom_id": r[0],
                "valence": round(float(r[1]), 4),
                "event_count": int(r[2]),
                "last_updated": r[3],
                "last_reason": r[4],
            }
            for r in rows
        ]
    finally:
        conn.close()


def stats() -> dict:
    _ensure_schema()
    conn = sqlite3.connect(str(BRAIN_DB))
    try:
        row = conn.execute(
            "SELECT count(*), "
            "SUM(CASE WHEN valence > 0 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN valence < 0 THEN 1 ELSE 0 END), "
            "AVG(valence), MIN(valence), MAX(valence) "
            "FROM atom_valence"
        ).fetchone()
        n, pos, neg, avg, lo, hi = row or (0, 0, 0, 0, 0, 0)
        return {
            "total": n or 0,
            "positive": pos or 0,
            "negative": neg or 0,
            "neutral": (n or 0) - (pos or 0) - (neg or 0),
            "avg_valence": round(avg or 0.0, 4),
            "range": [round(lo or 0.0, 4), round(hi or 0.0, 4)],
        }
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse
    import json as _json

    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")

    p_rec = sub.add_parser("record")
    p_rec.add_argument("atom_id")
    p_rec.add_argument("delta", type=float)
    p_rec.add_argument("--reason", default="")
    p_rec.add_argument("--source", default="cli")

    p_get = sub.add_parser("get")
    p_get.add_argument("atom_id")

    p_top = sub.add_parser("top")
    p_top.add_argument("--direction", choices=["positive", "negative", "both"], default="both")
    p_top.add_argument("--limit", type=int, default=20)

    sub.add_parser("stats")

    args = p.parse_args()
    if args.cmd == "record":
        print(
            _json.dumps(
                record_valence(args.atom_id, args.delta, reason=args.reason, source=args.source), indent=2
            )
        )
    elif args.cmd == "get":
        print(_json.dumps({"atom_id": args.atom_id, "valence": get_valence(args.atom_id)}, indent=2))
    elif args.cmd == "top":
        print(_json.dumps(top_valence(limit=args.limit, direction=args.direction), indent=2))
    elif args.cmd == "stats":
        print(_json.dumps(stats(), indent=2))
    else:
        p.print_help()
