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
from indexer import chroma_api, ensure_collection, _get_collection_id  # noqa: E402

SOURCE_COLLECTIONS = ["notes", "messages", "calendar", "tasks"]
TARGET = "personal"


def migrate(apply: bool = False):
    # Count source docs
    total = 0
    for col in SOURCE_COLLECTIONS:
        col_id = _get_collection_id(col)
        if not col_id:
            print(f"  {col}: not found")
            continue
        count = chroma_api("GET", f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/count")
        count = int(count) if isinstance(count, (int, str)) else 0
        print(f"  {col}: {count} docs")
        total += count

    print(f"\nTotal to migrate: {total} docs → '{TARGET}'")

    if not apply:
        print("\nRun with --apply to execute migration")
        return

    # Ensure target collection exists
    target_id = ensure_collection(TARGET)
    print(f"Target collection '{TARGET}': {target_id}")

    migrated = 0
    for col in SOURCE_COLLECTIONS:
        col_id = _get_collection_id(col)
        if not col_id:
            continue

        # Get all docs with embeddings and metadata
        count = chroma_api("GET", f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/count")
        count = int(count) if isinstance(count, (int, str)) else 0
        if count == 0:
            continue

        resp = chroma_api("POST", f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/get", {
            "limit": max(count, 100),
            "include": ["documents", "metadatas", "embeddings"],
        })

        ids = resp.get("ids", [])
        docs = resp.get("documents", [])
        metas = resp.get("metadatas", [])
        embs = resp.get("embeddings", [])

        if not ids:
            continue

        # Re-prefix IDs to avoid collisions
        new_ids = [f"personal:{_id.split(':', 1)[-1]}" if ':' in _id else f"personal:{_id}" for _id in ids]

        # Add source_type to metadata for filtering
        for m in metas:
            m["source_collection"] = col  # original collection name

        # Upsert in batches
        BATCH = 20
        for start in range(0, len(new_ids), BATCH):
            end = min(start + BATCH, len(new_ids))
            chroma_api("POST", f"/api/v2/tenants/default_tenant/databases/default_database/collections/{target_id}/upsert", {
                "ids": new_ids[start:end],
                "documents": docs[start:end],
                "metadatas": metas[start:end],
                "embeddings": embs[start:end],
            })
            migrated += (end - start)

        print(f"  {col}: migrated {len(ids)} docs")

    print(f"\nTotal migrated: {migrated} docs into '{TARGET}'")
    print(f"\nOld collections ({', '.join(SOURCE_COLLECTIONS)}) are still intact.")
    print("Delete them after verifying the migration works.")


if __name__ == "__main__":
    migrate(apply="--apply" in sys.argv)
