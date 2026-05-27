#!/usr/bin/env python3
"""Reconcile eval_holdout_lifecycle with eval_proposals + the pending JSON.

The holdout graduation pipeline relies on three coupled stores:
  * autonomy.db / eval_proposals — the source of truth for candidates
  * brain.db / eval_holdout_lifecycle — per-candidate eval_runs/eval_passes
  * cli/eval_holdout_pending.json — what eval_gate scores each night

Drift between them stalls auto-graduation. Observed 2026-05-19:
  - lifecycle has 2424 pending rows (no auto_stable_at, no rejected_at)
  - all 2424 are at eval_runs=0 because their source proposals are gone
    from eval_proposals (table only has 25 promoted + 305 rejected)
  - pending JSON is empty so _score_holdout_candidates iterates 0 rows
  - holdout_auto_graduation_7d sits at 0 indefinitely

This job:
  1. Drops lifecycle rows whose source proposal is missing or rejected
     (mark rejected_at + reason).
  2. Re-seeds the pending JSON from promoted+lifecycle-active intersect so
     nightly eval_gate scoring has candidates to exercise.
  3. Reports drift counts so the SLO dashboard sees the recovery.
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

log = logging.getLogger("brain.holdout_lifecycle_reconcile")

BRAIN_ROOT = Path("/Users/chrischo/server/brain")
BRAIN_DB = BRAIN_ROOT / "logs" / "brain.db"
AUTONOMY_DB = BRAIN_ROOT / "logs" / "autonomy.db"
PENDING_PATH = BRAIN_ROOT / "cli" / "eval_holdout_pending.json"

ORPHAN_REJECT_REASON = "source_proposal_missing_or_rejected"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _connect(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def reconcile(dry_run: bool = False) -> dict:
    summary: dict = {
        "lifecycle_pending_before": 0,
        "orphans_rejected": 0,
        "valid_promoted": 0,
        "repending_written": 0,
        "dry_run": dry_run,
        "status": "ok",
    }
    brain = _connect(BRAIN_DB)
    auto = _connect(AUTONOMY_DB)
    if brain is None or auto is None:
        summary["status"] = "db_missing"
        return summary
    try:
        pending = brain.execute(
            "SELECT candidate_id FROM eval_holdout_lifecycle "
            "WHERE auto_stable_at IS NULL AND rejected_at IS NULL"
        ).fetchall()
        summary["lifecycle_pending_before"] = len(pending)
        pending_ids = {row["candidate_id"] for row in pending if row["candidate_id"]}
        if not pending_ids:
            return summary

        chunk = list(pending_ids)
        valid: dict[str, dict] = {}
        # Sqlite parameter limit defaults to 999 — chunk to stay safe.
        for i in range(0, len(chunk), 800):
            batch = chunk[i : i + 800]
            placeholders = ",".join("?" * len(batch))
            rows = auto.execute(
                f"SELECT id, query, expected, expected_sources, status, source_event "  # noqa: S608 — fixed placeholder count
                f"FROM eval_proposals WHERE id IN ({placeholders})",
                batch,
            ).fetchall()
            for r in rows:
                if (r["status"] or "").lower() == "promoted":
                    valid[r["id"]] = dict(r)
        orphan_ids = sorted(pending_ids - set(valid.keys()))
        summary["valid_promoted"] = len(valid)

        if orphan_ids and not dry_run:
            now = _now_iso()
            brain.execute("BEGIN IMMEDIATE")
            try:
                brain.executemany(
                    "UPDATE eval_holdout_lifecycle SET rejected_at = ?, reject_reason = ? "
                    "WHERE candidate_id = ? AND auto_stable_at IS NULL AND rejected_at IS NULL",
                    [(now, ORPHAN_REJECT_REASON, oid) for oid in orphan_ids],
                )
                brain.commit()
            except Exception:
                brain.rollback()
                raise
        summary["orphans_rejected"] = len(orphan_ids) if not dry_run else 0
        summary["orphans_would_reject"] = len(orphan_ids) if dry_run else 0

        if valid:
            existing_payload: list = []
            if PENDING_PATH.exists():
                try:
                    raw = json.loads(PENDING_PATH.read_text())
                    if isinstance(raw, list):
                        existing_payload = raw
                except (OSError, json.JSONDecodeError):
                    existing_payload = []
            existing_ids = {e.get("id") for e in existing_payload if isinstance(e, dict) and e.get("id")}
            new_rows: list[dict] = []
            for cid, cand in valid.items():
                if cid in existing_ids:
                    continue
                try:
                    sources = json.loads(cand.get("expected_sources") or "[]")
                except (TypeError, json.JSONDecodeError):
                    sources = []
                new_rows.append(
                    {
                        "id": cid,
                        "query": cand.get("query") or "",
                        "expected": cand.get("expected") or "",
                        "expected_sources": sources,
                        "source_event": cand.get("source_event"),
                        "repended_at": _now_iso(),
                    }
                )
            if new_rows and not dry_run:
                merged = existing_payload + new_rows
                PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
                tmp = PENDING_PATH.with_suffix(PENDING_PATH.suffix + ".tmp")
                tmp.write_text(json.dumps(merged, indent=2, ensure_ascii=False))
                tmp.replace(PENDING_PATH)
            summary["repending_written"] = len(new_rows) if not dry_run else 0
            summary["repending_would_write"] = len(new_rows) if dry_run else 0
    finally:
        brain.close()
        auto.close()
    return summary


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    result = reconcile(dry_run=args.dry_run)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
