#!/usr/bin/env python3
"""Move legacy structural recall labels out of action_audit.outcome.

The structural recall judge now writes heuristic labels to
recall_structural_judgments so action_audit.outcome remains reserved for
manual/LLM truth judgments. This migration backfills old structural_* outcomes
into the sidecar and clears the legacy outcome fields.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BRAIN_DB = Path("/Users/chrischo/server/brain/logs/brain.db")
STRUCTURAL_OUTCOMES = ("structural_good", "structural_wrong", "structural_neutral")
_SCORE = {"structural_good": 1.0, "structural_wrong": 0.0, "structural_neutral": 0.5}


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_sidecar(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS recall_structural_judgments (
            action_audit_id INTEGER PRIMARY KEY,
            outcome TEXT NOT NULL,
            structural_score REAL NOT NULL,
            reason_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            judged_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_recall_structural_judgments_outcome
          ON recall_structural_judgments(outcome, created_at)
        """
    )


def _backup_db(db_path: Path, backup_path: Path) -> None:
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as src, sqlite3.connect(str(backup_path)) as dst:
        src.backup(dst)


def _legacy_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id, outcome, outcome_reason, created_at, resolved_at, route, actor, tool
        FROM action_audit
        WHERE outcome IN (?, ?, ?)
        ORDER BY id
        """,
        STRUCTURAL_OUTCOMES,
    ).fetchall()


def migrate(
    db_path: Path = BRAIN_DB, *, apply: bool = False, backup_path: Path | None = None
) -> dict[str, Any]:
    db_path = Path(db_path)
    if not db_path.exists():
        return {"ok": False, "error": f"db not found: {db_path}", "db_path": str(db_path)}

    now = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    if apply and backup_path is not None:
        _backup_db(db_path, backup_path)

    conn = _connect(db_path)
    try:
        rows = _legacy_rows(conn)
        counts: dict[str, int] = {}
        for row in rows:
            counts[str(row["outcome"])] = counts.get(str(row["outcome"]), 0) + 1
        if not apply:
            return {
                "ok": True,
                "applied": False,
                "db_path": str(db_path),
                "legacy_count": len(rows),
                "legacy_counts": counts,
                "backup_path": str(backup_path) if backup_path else None,
            }

        _ensure_sidecar(conn)
        inserted = 0
        cleared = 0
        with conn:
            for row in rows:
                reason = {
                    "source": "legacy_action_audit_outcome_backfill",
                    "legacy_outcome_reason": row["outcome_reason"],
                    "legacy_resolved_at": row["resolved_at"],
                    "route": row["route"],
                    "actor": row["actor"],
                    "tool": row["tool"],
                }
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO recall_structural_judgments
                      (action_audit_id, outcome, structural_score, reason_json, created_at, judged_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(row["id"]),
                        row["outcome"],
                        _SCORE[str(row["outcome"])],
                        json.dumps(reason, ensure_ascii=False, sort_keys=True),
                        row["created_at"] or now,
                        now,
                    ),
                )
                inserted += int(cur.rowcount or 0)
                cur = conn.execute(
                    """
                    UPDATE action_audit
                    SET outcome = NULL, outcome_reason = NULL, resolved_at = NULL
                    WHERE id = ? AND outcome IN ('structural_good','structural_wrong','structural_neutral')
                    """,
                    (int(row["id"]),),
                )
                cleared += int(cur.rowcount or 0)
        remaining = _legacy_rows(conn)
        return {
            "ok": True,
            "applied": True,
            "db_path": str(db_path),
            "legacy_count": len(rows),
            "legacy_counts": counts,
            "inserted_sidecar": inserted,
            "cleared_action_audit": cleared,
            "remaining_legacy_count": len(remaining),
            "backup_path": str(backup_path) if backup_path else None,
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=BRAIN_DB)
    parser.add_argument("--apply", action="store_true", help="perform the migration; default is dry-run")
    parser.add_argument(
        "--backup", type=Path, default=None, help="optional SQLite backup path before --apply"
    )
    parser.add_argument("--json", action="store_true", help="print JSON only")
    args = parser.parse_args()

    backup = args.backup
    if args.apply and backup is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = args.db.with_name(f"{args.db.name}.pre-structural-sidecar-{stamp}.bak")
    result = migrate(args.db, apply=args.apply, backup_path=backup)
    text = json.dumps(result, ensure_ascii=False, indent=None if args.json else 2, sort_keys=True)
    print(text)
    return 0 if result.get("ok") and (not args.apply or result.get("remaining_legacy_count") == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
