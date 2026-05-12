"""brain_core/atom_recall_quality.py — D7 predictive coding signal (per-atom).

confidence_calibration.py already fits a global Platt transform on outcome
pairs. That gives a SYSTEM-wide calibration but no per-atom feedback. If
atom X is recalled 50 times and judged wrong 40 times, the global Platt fit
can't surface that — the atom keeps appearing at its prior confidence.

This module aggregates action_audit (retrieved_atom_ids + outcome) into a
per-atom quality table:
  atom_recall_quality:
    atom_id      TEXT PK
    n_recalls    INTEGER
    n_good       INTEGER
    n_wrong      INTEGER
    n_restated   INTEGER
    accuracy     REAL    -- n_good / (n_good + n_wrong + n_restated)
    last_recalled_at TEXT
    last_computed_at TEXT

Read-only consumers (search ranking, atom QC dashboards) can downweight
atoms with low accuracy. Future passes can fold this into atom.confidence
directly via an explicit prediction-error rule. For now, the data simply
becomes visible.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db import ensure_schema, now_iso

try:
    from config import BRAIN_DB
except ImportError:
    BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")

log = logging.getLogger("brain.atom_recall_quality")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS atom_recall_quality (
    atom_id          TEXT PRIMARY KEY,
    n_recalls        INTEGER NOT NULL DEFAULT 0,
    n_good           INTEGER NOT NULL DEFAULT 0,
    n_wrong          INTEGER NOT NULL DEFAULT 0,
    n_restated       INTEGER NOT NULL DEFAULT 0,
    accuracy         REAL,
    last_recalled_at TEXT,
    last_computed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_arq_accuracy ON atom_recall_quality(accuracy);
CREATE INDEX IF NOT EXISTS idx_arq_recalls  ON atom_recall_quality(n_recalls);
"""


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotent schema init via shared ensure_schema."""
    ensure_schema(conn, "atom_recall_quality", _SCHEMA)


def _now_iso() -> str:
    """Backwards-compat shim — delegates to shared now_iso()."""
    return now_iso()


def run(days: int = 30) -> dict:
    """Rebuild atom_recall_quality from last `days` of action_audit data.

    Idempotent: replaces existing aggregates each run (cheap; the table is
    small relative to action_audit).
    """
    if not BRAIN_DB.exists():
        return {"status": "skip", "reason": "no_brain_db"}
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    conn = sqlite3.connect(str(BRAIN_DB), timeout=15)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT retrieved_atom_ids, outcome, created_at "
            "FROM action_audit "
            "WHERE outcome IS NOT NULL "
            "  AND route IN ('/recall/v2', '/recall/active') "
            "  AND created_at > ? "
            "  AND retrieved_atom_ids IS NOT NULL "
            "  AND retrieved_atom_ids != '[]'",
            (cutoff,),
        ).fetchall()

        # Tally per-atom counts
        tally: dict[str, dict] = {}
        for retrieved_json, outcome, ts in rows:
            try:
                atom_ids = json.loads(retrieved_json or "[]")
            except Exception:  # noqa: S112 — malformed audit row, skip
                continue
            if not isinstance(atom_ids, list):
                continue
            for aid in atom_ids:
                if not isinstance(aid, str) or not aid:
                    continue
                slot = tally.setdefault(
                    aid,
                    {
                        "n_recalls": 0,
                        "n_good": 0,
                        "n_wrong": 0,
                        "n_restated": 0,
                        "last_recalled_at": ts,
                    },
                )
                slot["n_recalls"] += 1
                if outcome == "judged_good":
                    slot["n_good"] += 1
                elif outcome == "judged_wrong":
                    slot["n_wrong"] += 1
                elif outcome == "restated":
                    slot["n_restated"] += 1
                if ts and ts > (slot.get("last_recalled_at") or ""):
                    slot["last_recalled_at"] = ts

        now = _now_iso()
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Wipe + repopulate. Cheaper + simpler than UPSERT for ~hundreds of atoms.
            conn.execute("DELETE FROM atom_recall_quality")
            for atom_id, c in tally.items():
                labeled = c["n_good"] + c["n_wrong"] + c["n_restated"]
                accuracy = (c["n_good"] / labeled) if labeled else None
                conn.execute(
                    "INSERT INTO atom_recall_quality "
                    "(atom_id, n_recalls, n_good, n_wrong, n_restated, accuracy, "
                    " last_recalled_at, last_computed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        atom_id,
                        c["n_recalls"],
                        c["n_good"],
                        c["n_wrong"],
                        c["n_restated"],
                        accuracy,
                        c["last_recalled_at"],
                        now,
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        # Summary metrics
        labeled_atoms = sum(1 for c in tally.values() if (c["n_good"] + c["n_wrong"] + c["n_restated"]) > 0)
        low_quality = sum(
            1
            for c in tally.values()
            if (c["n_good"] + c["n_wrong"] + c["n_restated"]) >= 3
            and c["n_good"] / (c["n_good"] + c["n_wrong"] + c["n_restated"]) < 0.3
        )
        return {
            "status": "ok",
            "atoms_seen": len(tally),
            "labeled_atoms": labeled_atoms,
            "low_quality_atoms": low_quality,
            "window_days": days,
        }
    finally:
        conn.close()


def get_atom_quality(atom_id: str) -> dict | None:
    if not BRAIN_DB.exists():
        return None
    conn = sqlite3.connect(str(BRAIN_DB), timeout=5)
    try:
        _ensure_schema(conn)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT atom_id, n_recalls, n_good, n_wrong, n_restated, accuracy, "
            "       last_recalled_at, last_computed_at "
            "FROM atom_recall_quality WHERE atom_id = ?",
            (atom_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "atom_id": row["atom_id"],
            "n_recalls": int(row["n_recalls"]),
            "n_good": int(row["n_good"]),
            "n_wrong": int(row["n_wrong"]),
            "n_restated": int(row["n_restated"]),
            "accuracy": float(row["accuracy"]) if row["accuracy"] is not None else None,
            "last_recalled_at": row["last_recalled_at"],
            "last_computed_at": row["last_computed_at"],
        }
    finally:
        conn.close()


def list_low_quality(limit: int = 50, min_labeled: int = 3, max_accuracy: float = 0.4) -> list[dict]:
    if not BRAIN_DB.exists():
        return []
    conn = sqlite3.connect(str(BRAIN_DB), timeout=5)
    try:
        _ensure_schema(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT atom_id, n_recalls, n_good, n_wrong, n_restated, accuracy "
            "FROM atom_recall_quality "
            "WHERE (n_good + n_wrong + n_restated) >= ? "
            "  AND accuracy IS NOT NULL "
            "  AND accuracy <= ? "
            "ORDER BY accuracy ASC, n_recalls DESC "
            "LIMIT ?",
            (min_labeled, max_accuracy, limit),
        ).fetchall()
        return [
            {
                "atom_id": r["atom_id"],
                "n_recalls": int(r["n_recalls"]),
                "n_good": int(r["n_good"]),
                "n_wrong": int(r["n_wrong"]),
                "n_restated": int(r["n_restated"]),
                "accuracy": round(float(r["accuracy"]), 4),
            }
            for r in rows
        ]
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=30)
    args = p.parse_args()
    print(json.dumps(run(days=args.days), indent=2, ensure_ascii=False))  # noqa: T201
