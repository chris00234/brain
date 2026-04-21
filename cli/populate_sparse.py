#!/opt/homebrew/bin/python3
"""cli/populate_sparse.py — populate the `sparse` slot on every point.

Three of the seven collections had their sparse slots drop during the
``rebuild_with_sparse.py`` reupsert path — the PointStruct(dense + sparse)
combination didn't persist the sparse vector for canonical, experience,
and obsidian. This script is idempotent: scroll each collection,
compute BM25 sparse from ``_document``, call ``update_vectors`` with
just the sparse slot. Safer than reupserting full points because we
don't touch the dense / contextual / raptor slots.

Usage:
    python cli/populate_sparse.py --dry-run
    python cli/populate_sparse.py
    python cli/populate_sparse.py --collection canonical
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))

from qdrant_client import QdrantClient  # noqa: E402
from qdrant_client.models import PointVectors, SparseVector  # noqa: E402
from sparse_tokenizer import SPARSE_TOKENIZER_VERSION, encode  # noqa: E402

sparse_encode = encode

PAGE = 500
BATCH = 200


def populate(client: QdrantClient, collection: str, *, dry_run: bool) -> dict:
    stats = {
        "collection": collection,
        "scanned": 0,
        "updated": 0,
        "skipped_empty_doc": 0,
        "skipped_current_version": 0,
    }
    print(f"\n=== {collection} ===")
    next_offset = None
    while True:
        pts, next_offset = client.scroll(
            collection_name=collection,
            limit=PAGE,
            offset=next_offset,
            with_payload=True,
            with_vectors=False,
        )
        if not pts:
            break

        updates: list[PointVectors] = []
        version_patches: list[tuple[str, dict]] = []
        for p in pts:
            stats["scanned"] += 1
            payload = p.payload or {}
            doc = payload.get("_document") or ""
            if not doc.strip():
                stats["skipped_empty_doc"] += 1
                continue
            # Smart skip: row already at current tokenizer version → leave it.
            if payload.get("sparse_tokenizer_version") == SPARSE_TOKENIZER_VERSION:
                stats["skipped_current_version"] += 1
                continue
            indices, values = sparse_encode(doc)
            if not indices:
                stats["skipped_empty_doc"] += 1
                continue
            updates.append(
                PointVectors(id=p.id, vector={"sparse": SparseVector(indices=indices, values=values)})
            )
            version_patches.append((p.id, {"sparse_tokenizer_version": SPARSE_TOKENIZER_VERSION}))

        # Flush in batches of BATCH to keep request size bounded.
        for start in range(0, len(updates), BATCH):
            chunk = updates[start : start + BATCH]
            patches_chunk = version_patches[start : start + BATCH]
            if dry_run:
                stats["updated"] += len(chunk)
            else:
                try:
                    client.update_vectors(collection_name=collection, points=chunk, wait=False)
                    # Stamp version so future runs can smart-skip these rows.
                    for pid, patch in patches_chunk:
                        client.set_payload(
                            collection_name=collection,
                            payload=patch,
                            points=[pid],
                            wait=False,
                        )
                    stats["updated"] += len(chunk)
                except Exception as e:
                    print(f"  update_vectors batch failed: {e}")

        print(
            f"  scanned={stats['scanned']} updated={stats['updated']} "
            f"skipped_empty={stats['skipped_empty_doc']} "
            f"skipped_current_version={stats['skipped_current_version']}"
        )

        if len(pts) < PAGE or not next_offset:
            break

    if not dry_run:
        time.sleep(1)

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--collection", help="Scope to one collection")
    parser.add_argument("--url", default="http://127.0.0.1:6333")
    args = parser.parse_args()

    client = QdrantClient(url=args.url, timeout=120)

    targets = (
        [args.collection]
        if args.collection
        else ["canonical", "semantic_memory", "experience", "knowledge", "code", "personal", "obsidian"]
    )

    grand = {"scanned": 0, "updated": 0, "skipped_empty_doc": 0}
    for t in targets:
        s = populate(client, t, dry_run=args.dry_run)
        grand["scanned"] += s["scanned"]
        grand["updated"] += s["updated"]
        grand["skipped_empty_doc"] += s["skipped_empty_doc"]

    print()
    print(
        f"TOTAL scanned={grand['scanned']} updated={grand['updated']} "
        f"skipped={grand['skipped_empty_doc']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
