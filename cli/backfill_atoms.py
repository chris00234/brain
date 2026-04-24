#!/usr/bin/env python3
"""backfill_atoms.py — retroactive v3 Brain Hygiene classification + entity
extraction for pre-migration atoms.

Walks brain.db::atoms where hygiene fields are still at defaults (never touched
by the ingest classifier), and for each atom:

  1. ingest_classifier.classify() — populates topic_key / speaker_entity /
     scope / provisional via one Sage LLM call.
  2. entity_graph.extract_and_store_entities() — walks the atom text through
     Sage and upserts Entity + RELATES_TO nodes + atom_entity links in one pass.
  3. UPDATE atoms SET ... — writes the classified fields back.

Resumable: tracks progress in logs/backfill_atoms_state.json so a crash or
interrupt doesn't re-spend LLM tokens on already-processed atoms.

~2s per atom x ~613 atoms = ~20 minutes. Run in background.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import UTC, datetime

sys.path.insert(0, "/Users/chrischo/server/brain/brain_core")

from config import BRAIN_LOGS_DIR

BRAIN_DB = BRAIN_LOGS_DIR / "brain.db"
STATE_FILE = BRAIN_LOGS_DIR / "backfill_atoms_state.json"
PROGRESS_EVERY = 10


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"processed_ids": [], "started_at": None, "updated_at": None}
    try:
        return json.loads(STATE_FILE.read_text())
    except json.JSONDecodeError:
        return {"processed_ids": [], "started_at": None, "updated_at": None}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.rename(STATE_FILE)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill v3 hygiene + entity extraction for existing atoms."
    )
    parser.add_argument("--limit", type=int, default=0, help="Max atoms to process (0=all)")
    parser.add_argument("--dry-run", action="store_true", help="Show count, don't call LLM")
    parser.add_argument("--skip-entities", action="store_true", help="Hygiene only (skip entity extraction)")
    parser.add_argument("--skip-classify", action="store_true", help="Entity only (skip classifier)")
    parser.add_argument(
        "--use-llm-classify",
        action="store_true",
        help="Use Sage LLM for classification (default: heuristic fallback, 10x faster)",
    )
    parser.add_argument(
        "--min-text-len", type=int, default=40, help="Skip atoms with text shorter than N chars"
    )
    args = parser.parse_args()

    state = _load_state()
    processed = set(state.get("processed_ids", []))
    if not state.get("started_at"):
        state["started_at"] = _now_iso()

    # Find candidates — atoms without hygiene classification (topic_key IS NULL)
    conn = sqlite3.connect(str(BRAIN_DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, chroma_id, text, kind, provenance_json FROM atoms "
        "WHERE (topic_key IS NULL OR topic_key = '') "
        "  AND tier != 'obsolete' "
        "  AND length(text) >= ? "
        "ORDER BY created_at ASC",
        (args.min_text_len,),
    ).fetchall()
    conn.close()

    candidates = [r for r in rows if r["id"] not in processed]
    total = len(candidates)
    if args.limit and total > args.limit:
        candidates = candidates[: args.limit]
        total = len(candidates)

    print(f"Candidates: {len(rows)} total, {len(processed)} already done, {total} to process")
    if args.dry_run:
        return 0

    # Lazy imports — keep dry-run fast
    from entity_graph import extract_and_store_entities
    from ingest_classifier import classify

    processed_this_run = 0
    errors = 0
    t_start = time.time()

    for i, row in enumerate(candidates, 1):
        atom_id = row["id"]
        text = row["text"] or ""
        kind = row["kind"] or "fact"
        provenance_raw = row["provenance_json"] or "{}"
        try:
            provenance = json.loads(provenance_raw)
        except json.JSONDecodeError:
            provenance = {}
        author_agent = provenance.get("agent", "claude")

        # Step 1: hygiene classifier — use heuristic mode in backfill to avoid
        # doubling the LLM cost (entity extraction already does 1 Sage call per
        # atom below). Heuristic is fast + conservative (marks all as
        # provisional=True which gets corrected on re-access via reinforcement).
        classification = None
        if not args.skip_classify:
            try:
                classification = classify(
                    text,
                    author_agent=author_agent,
                    category=kind,
                    use_llm=args.use_llm_classify,
                )
            except Exception as e:
                print(f"[{i}/{total}] {atom_id}: classify error: {e}", file=sys.stderr)
                errors += 1

        # Step 2: entity extraction (best-effort, fire-and-forget is not possible
        # in a backfill because we want progress tracking, so run inline)
        if not args.skip_entities:
            try:
                extract_and_store_entities(text[:1500], row["chroma_id"] or atom_id)
            except Exception as e:
                print(f"[{i}/{total}] {atom_id}: entity error: {e}", file=sys.stderr)
                errors += 1

        # Step 3: update atom hygiene fields
        if classification:
            try:
                conn = sqlite3.connect(str(BRAIN_DB))
                conn.execute(
                    "UPDATE atoms SET "
                    "  topic_key = ?, "
                    "  speaker_entity = ?, "
                    "  scope = ?, "
                    "  provisional = ?, "
                    "  trust_score = ?, "
                    "  updated_at = ? "
                    "WHERE id = ?",
                    (
                        classification.topic_key,
                        classification.speaker_entity,
                        classification.scope,
                        1 if classification.provisional else 0,
                        classification.confidence,
                        _now_iso(),
                        atom_id,
                    ),
                )
                conn.commit()
                conn.close()
            except sqlite3.Error as e:
                print(f"[{i}/{total}] {atom_id}: sql update error: {e}", file=sys.stderr)
                errors += 1

        processed.add(atom_id)
        processed_this_run += 1

        if i % PROGRESS_EVERY == 0 or i == total:
            elapsed = time.time() - t_start
            rate = processed_this_run / elapsed if elapsed > 0 else 0
            remaining_s = (total - i) / rate if rate > 0 else 0
            print(
                f"[{i}/{total}] atom={atom_id[:20]} "
                f"rate={rate:.2f}/s elapsed={elapsed:.0f}s "
                f"eta={remaining_s:.0f}s errors={errors}",
                flush=True,
            )
            # Checkpoint state every PROGRESS_EVERY atoms
            state["processed_ids"] = list(processed)
            state["updated_at"] = _now_iso()
            _save_state(state)

    # Final checkpoint
    state["processed_ids"] = list(processed)
    state["updated_at"] = _now_iso()
    _save_state(state)

    elapsed = time.time() - t_start
    print(f"\nBackfill complete: processed={processed_this_run} errors={errors} elapsed={elapsed:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
