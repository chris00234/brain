#!/opt/homebrew/bin/python3
"""cli/populate_canonical_contextual.py — fill the `contextual` named vector.

Canonical collection was bootstrapped with three named vectors on every
point (`dense`, `contextual`, `raptor`). The Chroma→Qdrant migration only
copied the single Chroma embedding into the `dense` slot, leaving
`contextual` empty on contextualized rows and `raptor` empty everywhere.

This script populates `contextual` for rows where `payload.contextualized = True`
by re-embedding `contextual_prefix + "\\n\\n" + document` and writing the
vector into the named `contextual` slot via Qdrant's `update_vectors` API.

Usage:
    python cli/populate_canonical_contextual.py --dry-run
    python cli/populate_canonical_contextual.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))

from qdrant_client import QdrantClient  # noqa: E402
from qdrant_client.models import (  # noqa: E402
    FieldCondition,
    Filter,
    MatchValue,
    PointVectors,
)

PAGE = 200


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--url", default="http://127.0.0.1:6333")
    args = parser.parse_args()

    client = QdrantClient(url=args.url, timeout=60)

    from indexer import get_embeddings_batch

    total = 0
    updated = 0
    failed = 0
    next_offset = None
    scroll_filter = Filter(must=[FieldCondition(key="contextualized", match=MatchValue(value=True))])

    while True:
        pts, next_offset = client.scroll(
            collection_name="canonical",
            scroll_filter=scroll_filter,
            limit=PAGE,
            offset=next_offset,
            with_payload=True,
            with_vectors=False,
        )
        if not pts:
            break
        total += len(pts)

        batch_ids: list = []
        batch_texts: list[str] = []
        for p in pts:
            payload = p.payload or {}
            prefix = payload.get("contextual_prefix") or ""
            doc = payload.get("_document") or ""
            if not prefix or not doc:
                failed += 1
                continue
            batch_ids.append(p.id)
            # Match the original semantic format indexer builds for contextualized chunks.
            batch_texts.append(f"{prefix}\n\n{doc}")

        if not batch_ids:
            if len(pts) < PAGE:
                break
            continue

        print(f"  embedding {len(batch_ids)} rows...")
        embs = get_embeddings_batch(batch_texts, prefix="passage", use_cache=True)

        point_vectors = []
        for pid, emb in zip(batch_ids, embs, strict=True):
            if not emb:
                failed += 1
                continue
            point_vectors.append(PointVectors(id=pid, vector={"contextual": emb}))

        if args.dry_run:
            print(f"  [dry-run] would update {len(point_vectors)} contextual vectors")
        elif point_vectors:
            client.update_vectors(collection_name="canonical", points=point_vectors, wait=False)
            updated += len(point_vectors)
            print(f"  updated {len(point_vectors)}")

        if len(pts) < PAGE or not next_offset:
            break

    print()
    print(f"total contextualized rows scanned: {total}")
    print(f"contextual slot populated:         {updated}")
    print(f"failed (missing prefix or embed):  {failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
