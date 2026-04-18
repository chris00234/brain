"""brain_core/entity_reconcile.py — nightly catch-up for missed entity extractions.

The live ingest path in atoms_store.upsert_atom fires entity extraction via
the bounded _submit_bg_extract pool. When the inflight cap is hit (F3),
extractions are DROPPED and _BG_EXTRACT_DROPPED counts them — this is the
right call for hot-path stability but means the entity graph can drift from
the atoms table over time.

This job runs nightly to find atoms that have no entity_graph presence and
re-runs extraction for them. Idempotent: atoms with existing Entity nodes
are skipped by extract_and_store_entities's internal dedup.

Invocation:
  from entity_reconcile import run
  run()  # returns {"reconciled": int, "skipped": int, "errors": int}

Bounded work: processes at most MAX_PER_RUN atoms per invocation so a long
backlog can't stall the nightly pipeline. Prefers atoms created in the last
7 days so fresh content catches up first; anything older becomes "historical
backfill" that can run explicitly.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

log = logging.getLogger("brain.entity_reconcile")

MAX_PER_RUN = int(os.environ.get("BRAIN_ENTITY_RECONCILE_MAX", "200"))
MIN_TEXT_LEN = 40
FRESH_DAYS = 7


def _find_missing_atoms() -> list[tuple[str, str]]:
    """Return (atom_id, text) pairs for recent atoms with no entity graph rows.

    An atom "has entities" if atom_entity has at least one row for it OR the
    Neo4j MemoryAccess node for its chroma_id exists. We use the SQLite mirror
    (atom_entity) as the primary signal because it's cheap to query; atoms
    with entities in Neo4j but not the mirror will be caught here too (and
    extraction is idempotent, so re-running is safe).
    """
    try:
        from atoms_store import _conn
    except ImportError:
        return []

    rows: list[tuple[str, str]] = []
    try:
        with _conn() as c:
            cursor = c.execute(
                """
                SELECT a.id, a.text
                FROM atoms a
                LEFT JOIN atom_entity ae ON ae.atom_id = a.id
                WHERE ae.atom_id IS NULL
                  AND length(a.text) >= ?
                  AND a.tier != 'obsolete'
                  AND a.created_at >= datetime('now', ?)
                ORDER BY a.created_at DESC
                LIMIT ?
                """,
                (MIN_TEXT_LEN, f"-{FRESH_DAYS} days", MAX_PER_RUN),
            )
            for row in cursor:
                rows.append((row["id"], row["text"] or ""))
    except Exception as e:
        log.warning("entity_reconcile query failed: %s", e)
    return rows


def _extract_for_atom(atom_id: str, text: str) -> bool:
    """Run extract_and_store_entities for an atom. Returns True on success."""
    try:
        from entity_graph import extract_and_store_entities

        extract_and_store_entities(text[:1500], atom_id)
        return True
    except Exception as e:
        log.debug("reconcile extract failed for %s: %s", atom_id[:16], e)
        return False


def run() -> dict:
    """Entry point for scheduler. Catches up entity extractions that the
    hot-path bounded pool dropped. Synchronous — runs extractions serially
    to avoid re-triggering the same resource pressure that caused the drops.
    """
    t0 = time.time()
    missing = _find_missing_atoms()
    if not missing:
        return {
            "status": "ok",
            "reconciled": 0,
            "skipped": 0,
            "errors": 0,
            "candidates": 0,
            "latency_ms": int((time.time() - t0) * 1000),
        }

    reconciled = 0
    errors = 0
    for atom_id, text in missing:
        if _extract_for_atom(atom_id, text):
            reconciled += 1
        else:
            errors += 1
        # small sleep so we don't re-create the burst that dropped them
        time.sleep(0.05)

    return {
        "status": "ok",
        "reconciled": reconciled,
        "errors": errors,
        "candidates": len(missing),
        "latency_ms": int((time.time() - t0) * 1000),
    }


if __name__ == "__main__":
    import json

    print(json.dumps(run()))
