#!/opt/homebrew/bin/python3
"""Migrate notes/messages/calendar/tasks → unified 'personal' collection.

Copies all documents from 4 source collections into a single 'personal' collection,
preserving metadata (the 'type' field already distinguishes note/message/event/reminder).

Usage:
  migrate_personal.py              # dry-run
  migrate_personal.py --apply      # execute migration
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "brain_core"))
from vector_store import get_vector_store

SOURCE_COLLECTIONS = ["notes", "messages", "calendar", "tasks"]
TARGET = "personal"


def migrate(apply: bool = False):
    store = get_vector_store()

    # Count source docs
    total = 0
    for col in SOURCE_COLLECTIONS:
        count = store.count(col)
        if count == 0:
            print(f"  {col}: not found or empty")
            continue
        print(f"  {col}: {count} docs")
        total += count

    print(f"\nTotal to migrate: {total} docs → '{TARGET}'")

    if not apply:
        print("\nRun with --apply to execute migration")
        return

    store.create_collection(TARGET)
    print(f"Target collection '{TARGET}': ensured")

    migrated = 0
    for col in SOURCE_COLLECTIONS:
        count = store.count(col)
        if count == 0:
            continue

        points = store.get(
            col,
            limit=max(count, 100),
            with_payload=True,
            with_documents=True,
            with_vectors=True,
        )
        if not points:
            continue

        # Re-prefix IDs to avoid collisions in the target collection.
        new_ids = [
            f"personal:{p.id.split(':', 1)[-1]}" if ":" in p.id else f"personal:{p.id}"
            for p in points
        ]
        docs = [p.document or "" for p in points]
        metas = [dict(p.payload) for p in points]
        embs = [p.vector or [] for p in points]
        # Tag source for filtering
        for m in metas:
            m["source_collection"] = col

        # Upsert in batches
        BATCH = 20
        for start in range(0, len(new_ids), BATCH):
            end = min(start + BATCH, len(new_ids))
            store.upsert(
                TARGET,
                ids=new_ids[start:end],
                vectors=embs[start:end],
                documents=docs[start:end],
                payloads=metas[start:end],
            )
            migrated += end - start

        print(f"  {col}: migrated {len(points)} docs")

    print(f"\nTotal migrated: {migrated} docs into '{TARGET}'")
    print(f"\nOld collections ({', '.join(SOURCE_COLLECTIONS)}) are still intact.")
    print("Delete them after verifying the migration works.")


if __name__ == "__main__":
    migrate(apply="--apply" in sys.argv)
