#!/usr/bin/env python3
"""Drop ghost atom_deboost rows whose atom_id no longer exists in Qdrant.

The atom_deboost table is populated from action_audit.retrieved_chroma_ids
via _to_dashed_uuid normalization (see brain_core/routes/recall.py). When
the upstream points get deleted or migrated, the deboost rows become
orphans — they reference IDs that no longer exist in any Qdrant collection
and can therefore never match a live recall result. As of 2026-05-19 all
323 deboost rows are orphans, which means the entire wrong-recall feedback
signal is currently invisible to the recall path.

This cleanup:
  1. Loads every distinct atom_id from atom_deboost.
  2. Probes each Qdrant collection to see whether the point still exists.
  3. Deletes rows whose atom_id is unresolvable.
  4. Logs counts to logs/atom_deboost_cleanup.jsonl.

Note: this does NOT solve the underlying ID-schema mismatch that makes
deboost a no-op even when atoms ARE live — the result.id format and the
audit/deboost format diverge. Cleanup just keeps the table from carrying
dead state. Full resolution requires unifying ID schemas across the
audit / deboost / recall paths.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

log = logging.getLogger("brain.atom_deboost_cleanup")

BRAIN_ROOT = Path("/Users/chrischo/server/brain")
BRAIN_DB = BRAIN_ROOT / "logs" / "brain.db"
AUDIT_LOG = BRAIN_ROOT / "logs" / "atom_deboost_cleanup.jsonl"
COLLECTIONS = (
    "canonical",
    "experience",
    "code",
    "distilled",
    "knowledge",
    "semantic_memory",
    "obsidian",
    "personal",
    "healthcheck_probe",
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def cleanup(dry_run: bool = False) -> dict:
    summary: dict = {
        "checked": 0,
        "ghost": 0,
        "live": 0,
        "deleted": 0,
        "dry_run": dry_run,
        "started_at": _now_iso(),
    }
    if not BRAIN_DB.exists():
        summary["status"] = "db_missing"
        return summary
    try:
        from qdrant_client import QdrantClient
    except ImportError:
        summary["status"] = "qdrant_client_unavailable"
        return summary

    conn = sqlite3.connect(str(BRAIN_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT atom_id FROM atom_deboost").fetchall()
    except sqlite3.OperationalError as exc:
        summary["status"] = f"table_unavailable: {exc}"
        conn.close()
        return summary
    ids = [row["atom_id"] for row in rows if row["atom_id"]]
    summary["checked"] = len(ids)
    if not ids:
        conn.close()
        return {**summary, "status": "empty_table"}

    client = QdrantClient(url="http://127.0.0.1:6333")
    ghost_ids: list[str] = []
    for aid in ids:
        found = False
        for col in COLLECTIONS:
            try:
                pts = client.retrieve(collection_name=col, ids=[aid], with_payload=False, with_vectors=False)
                if pts:
                    found = True
                    break
            except Exception:
                continue
        if found:
            summary["live"] += 1
        else:
            ghost_ids.append(aid)
            summary["ghost"] += 1

    if ghost_ids and not dry_run:
        chunk = list(ghost_ids)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for i in range(0, len(chunk), 500):
                batch = chunk[i : i + 500]
                placeholders = ",".join("?" * len(batch))
                conn.execute(
                    f"DELETE FROM atom_deboost WHERE atom_id IN ({placeholders})",  # noqa: S608 — fixed placeholder count
                    batch,
                )
            conn.commit()
            summary["deleted"] = len(ghost_ids)
        except Exception as exc:
            conn.rollback()
            summary["status"] = f"delete_failed: {exc}"
    elif dry_run:
        summary["would_delete"] = len(ghost_ids)
    conn.close()

    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({**summary, "finished_at": _now_iso()}) + "\n")
    summary["finished_at"] = _now_iso()
    summary.setdefault("status", "ok")
    return summary


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    result = cleanup(dry_run=args.dry_run)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
