#!/Users/chrischo/server/brain/.venv/bin/python
"""backfill_atom_evidence.py — Phase N2 one-shot backfill.

Every atom written before Phase N2 (brain_db@7) has zero rows in
atom_evidence — get_confidence_history returns an empty list, and the
/brain/atoms/{id}/history endpoint shows nothing. This is cosmetic but
confusing: a brand-new atom and a 6-month-old atom look identical.

This script walks every atom and, for each one that has no evidence row,
inserts a single baseline `event_type='manual' weight=0` row referencing
'n2_backfill'. Confidence is NOT touched — weight=0 is a no-op under the
logit-space update. Ledger becomes rational; rollback_confidence(atom_id,
back_to_event_id=0) returns 0.5 exactly as the math expects.

Idempotent — every atom gets at most one backfill row. Safe to re-run.

Usage:
    .venv/bin/python cli/backfill_atom_evidence.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")

BACKFILL_REF = "n2_backfill"


def run(dry_run: bool = False) -> dict:
    if not BRAIN_DB.exists():
        return {"error": "brain.db missing"}
    conn = sqlite3.connect(str(BRAIN_DB))
    conn.row_factory = sqlite3.Row
    try:
        total_atoms = conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0]
        missing = conn.execute(
            "SELECT a.id FROM atoms a " "LEFT JOIN atom_evidence e ON e.atom_id = a.id " "WHERE e.id IS NULL"
        ).fetchall()
        missing_ids = [r["id"] for r in missing]
        if dry_run:
            return {
                "total_atoms": total_atoms,
                "missing_count": len(missing_ids),
                "dry_run": True,
                "sample": missing_ids[:5],
            }
        if not missing_ids:
            return {"total_atoms": total_atoms, "missing_count": 0, "inserted": 0}
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.executemany(
            "INSERT INTO atom_evidence "
            "(atom_id, event_type, weight, evidence_ref, cluster_size, created_at) "
            "VALUES (?, 'manual', 0, ?, 1, datetime('now'))",
            [(aid, BACKFILL_REF) for aid in missing_ids],
        )
        conn.commit()
        return {
            "total_atoms": total_atoms,
            "missing_count": len(missing_ids),
            "inserted": cur.rowcount,
        }
    finally:
        conn.close()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    result = run(dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
