#!/usr/bin/env python3
"""cli/cleanup_test_atoms.py — mark obvious test atoms tier='obsolete'.

The 2026-04-26 deep audit found three test atoms in production with high
confidence (notably "Chris uses a special test framework called
QUUXFRAMEWORK9999" at confidence 0.98). These are test patterns that
slipped past the atoms_gate scanner and now sit in the truth layer.

This script targets ONLY explicit test patterns:
  - 'QUUXFRAMEWORK%'
  - 'Round_ final verification%'
  - 'placeholder_test_%'

Safety:
  - Dry-run by default; --apply required to actually mark obsolete.
  - Refuses to obsolete atoms with reinforcement_count > 0.
  - Audits every change via audit_log.

Usage:
  python cli/cleanup_test_atoms.py --dry-run    # default
  python cli/cleanup_test_atoms.py --apply
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")

TEST_PATTERNS: list[tuple[str, str]] = [
    ("%QUUXFRAMEWORK%", "test framework string"),
    ("Round_ final verification%", "round-N test verification line"),
    ("%placeholder_test_%", "placeholder fixture"),
]


def find_candidates() -> list[dict]:
    if not BRAIN_DB.exists():
        return []
    conn = sqlite3.connect(str(BRAIN_DB))
    try:
        rows: list[dict] = []
        for pat, reason in TEST_PATTERNS:
            cur = conn.execute(
                "SELECT id, kind, ROUND(confidence, 2) AS conf, "
                "       reinforcement_count AS rc, substr(text, 1, 100) AS preview "
                "FROM atoms "
                "WHERE tier != 'obsolete' AND text LIKE ?",
                (pat,),
            )
            for row in cur.fetchall():
                rows.append(
                    {
                        "id": row[0],
                        "kind": row[1],
                        "confidence": row[2],
                        "reinforcement_count": row[3],
                        "preview": row[4],
                        "matched_pattern": pat,
                        "reason": reason,
                    }
                )
        return rows
    finally:
        conn.close()


def apply_obsolete(candidates: list[dict]) -> dict:
    if not candidates:
        return {"applied": 0, "skipped": 0, "rows": []}
    now = datetime.now(UTC).isoformat(timespec="seconds")
    applied: list[str] = []
    skipped: list[dict] = []
    conn = sqlite3.connect(str(BRAIN_DB))
    try:
        conn.execute("BEGIN IMMEDIATE")
        for c in candidates:
            if c["reinforcement_count"] > 0:
                skipped.append(c)
                continue
            conn.execute(
                "UPDATE atoms SET tier = 'obsolete', updated_at = ? WHERE id = ?",
                (now, c["id"]),
            )
            applied.append(c["id"])
        conn.commit()
    finally:
        conn.close()

    # Best-effort audit log.
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
        from audit_log import log_event

        for cid in applied:
            log_event(
                event_type="atom_obsolete",
                source="cli/cleanup_test_atoms.py",
                payload={"atom_id": cid, "reason": "test_pattern_match"},
            )
    except Exception:  # noqa: S110 — audit log is best-effort
        pass
    return {"applied": len(applied), "skipped": len(skipped), "rows": applied}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="actually mark obsolete (default: dry-run)")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    args = parser.parse_args()

    candidates = find_candidates()
    summary: dict = {
        "candidates": len(candidates),
        "patterns": [p[0] for p in TEST_PATTERNS],
        "rows": candidates,
    }

    if args.apply:
        summary["result"] = apply_obsolete(candidates)
    else:
        summary["dry_run"] = True

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
