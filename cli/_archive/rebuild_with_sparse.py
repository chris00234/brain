#!/opt/homebrew/bin/python3
"""cli/rebuild_with_sparse.py — add BM25 sparse vectors to all 7 collections.

Qdrant 1.17's `update_collection` cannot add new sparse-vector names to an
existing collection — the only path is recreate. This script migrates each
of the 7 collections through a delete → recreate → reupsert cycle, with
the sparse vector computed at upsert time via
``brain_core.sparse_tokenizer``.

Preserves existing dense + contextual (+ raptor for canonical) vectors
by scrolling them out before deletion. Payloads carry over unchanged.

Brief per-collection downtime during the delete->create gap (~1-3 seconds).
Brain queries against a mid-flight collection return empty; the 7-shard
architecture means only one collection is affected at a time.

Usage:
    python cli/rebuild_with_sparse.py --dry-run
    python cli/rebuild_with_sparse.py
    python cli/rebuild_with_sparse.py --collection canonical
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))

from qdrant_client import QdrantClient  # noqa: E402
from qdrant_client.models import (  # noqa: E402
    Distance,
    HnswConfigDiff,
    Modifier,
    PointStruct,
    ScalarQuantization,
    ScalarQuantizationConfig,
    ScalarType,
    SparseIndexParams,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)
from sparse_tokenizer import encode as sparse_encode  # noqa: E402

VECTOR_SIZE = 1024
PAGE = 200


def _vector_params() -> VectorParams:
    return VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE, on_disk=True)


# Named vector schemas per collection. Canonical keeps dense + contextual +
# raptor; others just dense. All gain a sparse slot.
NAMED_VECTOR_SCHEMA: dict[str, dict[str, VectorParams]] = {
    "canonical": {
        "dense": _vector_params(),
        "contextual": _vector_params(),
        "raptor": _vector_params(),
    },
    "semantic_memory": {"dense": _vector_params()},
    "experience": {"dense": _vector_params()},
    "knowledge": {"dense": _vector_params()},
    "code": {"dense": _vector_params()},
    "personal": {"dense": _vector_params()},
    "obsidian": {"dense": _vector_params()},
}

SPARSE_CONFIG = {
    "sparse": SparseVectorParams(
        index=SparseIndexParams(on_disk=False),
        modifier=Modifier.IDF,
    ),
}

QUANTIZATION = ScalarQuantization(
    scalar=ScalarQuantizationConfig(type=ScalarType.INT8, quantile=0.99, always_ram=True)
)
HNSW = HnswConfigDiff(m=16, ef_construct=128, full_scan_threshold=10000, on_disk=False)


def _fetch_all_points(client: QdrantClient, collection: str, wanted_vectors: list[str]) -> list[dict]:
    """Scroll every point out, returning a list of serializable dicts.

    We capture point id + named vectors + payload. Document (_document in
    payload) is carried within payload, no separate handling needed.
    """
    rows: list[dict] = []
    next_offset = None
    fetched = 0
    while True:
        pts, next_offset = client.scroll(
            collection_name=collection,
            limit=PAGE,
            offset=next_offset,
            with_payload=True,
            with_vectors=wanted_vectors,
        )
        if not pts:
            break
        for p in pts:
            vec = p.vector if isinstance(p.vector, dict) else {}
            rows.append(
                {
                    "id": p.id,
                    "vector": {k: list(v) for k, v in vec.items() if v and any(v)},
                    "payload": dict(p.payload or {}),
                }
            )
        fetched += len(pts)
        if len(pts) < PAGE or not next_offset:
            break
    print(f"    scrolled: {fetched}")
    return rows


def _index_payloads(client: QdrantClient, collection: str) -> None:
    """Re-apply payload indexes after collection recreation."""
    from qdrant_bootstrap import SCHEMAS  # type: ignore[import-not-found]

    spec = SCHEMAS.get(collection, {})
    for field, schema_type in spec.get("payload_indexes", []):
        try:
            client.create_payload_index(
                collection_name=collection,
                field_name=field,
                field_schema=schema_type,
            )
        except Exception as exc:
            msg = str(exc).lower()
            if "already" not in msg and "conflict" not in msg:
                print(f"    WARN: index {collection}.{field} failed: {exc}")


def rebuild_collection(client: QdrantClient, collection: str, *, dry_run: bool) -> dict:
    stats = {"collection": collection, "scrolled": 0, "upserted": 0, "errors": 0}
    schema = NAMED_VECTOR_SCHEMA[collection]
    wanted_vectors = list(schema.keys())

    print(f"\n=== {collection} ===")
    try:
        old_count = client.count(collection, exact=True).count
    except Exception as e:
        print(f"  {collection}: count failed: {e}")
        stats["errors"] += 1
        return stats
    print(f"  existing count: {old_count}")

    if old_count == 0:
        print("  empty; skip")
        return stats

    print("  [1/4] scrolling all points...")
    rows = _fetch_all_points(client, collection, wanted_vectors)
    stats["scrolled"] = len(rows)

    if dry_run:
        print(f"  [dry-run] would delete + recreate {collection} with sparse slot")
        print(f"  [dry-run] would reupsert {len(rows)} points with BM25 sparse computed")
        return stats

    print("  [2/4] deleting old collection...")
    client.delete_collection(collection_name=collection)

    print("  [3/4] creating new collection with sparse slot...")
    client.create_collection(
        collection_name=collection,
        vectors_config=schema,
        sparse_vectors_config=SPARSE_CONFIG,
        quantization_config=QUANTIZATION,
        hnsw_config=HNSW,
    )
    _index_payloads(client, collection)

    print("  [4/4] reupserting with BM25 sparse...")
    BATCH = 100
    for start in range(0, len(rows), BATCH):
        batch = rows[start : start + BATCH]
        points: list[PointStruct] = []
        for row in batch:
            doc = (row["payload"] or {}).get("_document") or ""
            indices, values = sparse_encode(doc)
            vectors = dict(row["vector"])
            if indices:
                vectors["sparse"] = SparseVector(indices=indices, values=values)
            points.append(PointStruct(id=row["id"], vector=vectors, payload=row["payload"]))
        try:
            # wait=True: this script deletes the old collection before
            # re-upserting, so any un-ack'd write lost to a crash or OOM
            # is permanently gone. Per-batch sync is the right tradeoff
            # for a one-shot migration.
            client.upsert(collection_name=collection, points=points, wait=True)
            stats["upserted"] += len(points)
        except Exception as e:
            print(f"    upsert batch at {start} failed: {e}")
            stats["errors"] += 1
        if (start // BATCH) % 10 == 0:
            print(f"    batch {start // BATCH + 1}: upserted {stats['upserted']}")

    # Wait for indexing to settle before verification
    time.sleep(1)
    new_count = client.count(collection, exact=True).count
    print(f"  verify: old={old_count} new={new_count} match={'OK' if new_count == old_count else 'MISMATCH'}")
    if new_count != old_count:
        stats["errors"] += 1
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--collection", help="Scope to one collection")
    parser.add_argument("--url", default="http://127.0.0.1:6333")
    args = parser.parse_args()

    client = QdrantClient(url=args.url, timeout=120)
    targets = [args.collection] if args.collection else list(NAMED_VECTOR_SCHEMA.keys())
    for t in targets:
        if t not in NAMED_VECTOR_SCHEMA:
            print(f"ERROR: unknown collection {t!r}", file=sys.stderr)
            return 2

    grand = {"scrolled": 0, "upserted": 0, "errors": 0}
    for t in targets:
        s = rebuild_collection(client, t, dry_run=args.dry_run)
        grand["scrolled"] += s["scrolled"]
        grand["upserted"] += s["upserted"]
        grand["errors"] += s["errors"]

    print()
    print(f"TOTAL scrolled={grand['scrolled']} upserted={grand['upserted']} errors={grand['errors']}")
    return 0 if grand["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
