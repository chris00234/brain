#!/usr/bin/env python3
"""Build source-aware v2 shadow Qdrant collections.

This is the safe full-quality rebuild path:
- production collections are left untouched;
- shadow collections are rebuilt with v2 entry metadata;
- natural document collections use source-aware collectors where available;
- atomic/code/memory collections re-embed existing point documents while
  preserving source-native boundaries.

Promotion is intentionally separate from this script.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

BRAIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))
sys.path.insert(0, str(BRAIN_ROOT / "cli"))

# Force semantic-capable collectors to use semantic boundaries for eligible
# prose. Atomic/code/turn-based sources still stay source-native via policy.
os.environ.setdefault("BRAIN_SEMANTIC_CHUNKING", "1")

from indexer import (  # noqa: E402
    add_documents,
    collect_canonical,
    collect_experience,
    collect_obsidian,
    get_embeddings_batch,
)
from qdrant_bootstrap import HNSW, QUANTIZATION, SCHEMAS, SPARSE_CONFIG  # noqa: E402
from qdrant_client import QdrantClient  # noqa: E402
from qdrant_client.models import Distance, PayloadSchemaType, VectorParams  # noqa: E402
from source_policy import enrich_payload_for_entry  # noqa: E402
from vector_store import get_vector_store  # noqa: E402

DEFAULT_COLLECTIONS = [
    "canonical",
    "distilled",
    "obsidian",
    "experience",
    "knowledge",
    "code",
    "personal",
    "semantic_memory",
]

COLLECTOR_COLLECTIONS = {"canonical", "distilled", "obsidian", "experience"}
COPY_REEMBED_COLLECTIONS = {"knowledge", "code", "personal", "semantic_memory"}


def shadow_name(collection: str, suffix: str) -> str:
    return f"{collection}__{suffix}"


def _client(url: str) -> QdrantClient:
    return QdrantClient(url=url, timeout=60, check_compatibility=False)


def _create_shadow_collection(client: QdrantClient, base: str, shadow: str, *, reset: bool) -> None:
    existing = {c.name for c in (client.get_collections().collections or [])}
    if shadow in existing and reset:
        client.delete_collection(collection_name=shadow)
        existing.remove(shadow)
    if shadow not in existing:
        spec = SCHEMAS.get(base)
        if spec:
            client.create_collection(
                collection_name=shadow,
                vectors_config=spec["vectors"],
                sparse_vectors_config=SPARSE_CONFIG,
                quantization_config=QUANTIZATION,
                hnsw_config=HNSW,
            )
        else:
            client.create_collection(
                collection_name=shadow,
                vectors_config={"dense": VectorParams(size=1024, distance=Distance.COSINE, on_disk=True)},
                sparse_vectors_config=SPARSE_CONFIG,
                quantization_config=QUANTIZATION,
                hnsw_config=HNSW,
            )
    # Index creation is idempotent; tolerate already-exists conflicts.
    index_fields = (SCHEMAS.get(base) or {}).get("payload_indexes") or []
    fallback_indexes = [
        ("schema_version", PayloadSchemaType.KEYWORD),
        ("chunk_strategy", PayloadSchemaType.KEYWORD),
        ("source_type", PayloadSchemaType.KEYWORD),
        ("source_kind", PayloadSchemaType.KEYWORD),
        ("tags", PayloadSchemaType.KEYWORD),
        ("document_id", PayloadSchemaType.KEYWORD),
        ("content_hash", PayloadSchemaType.KEYWORD),
    ]
    for field, schema in [*index_fields, *fallback_indexes]:
        try:
            client.create_payload_index(collection_name=shadow, field_name=field, field_schema=schema)
        except Exception as exc:
            if "already" not in str(exc).lower() and "conflict" not in str(exc).lower():
                print(f"  WARN: index {shadow}.{field} failed: {exc}")


def _collector_docs(collection: str) -> list[dict[str, Any]]:
    if collection in {"canonical", "distilled"}:
        docs = collect_canonical()
        wanted_type = f"{collection}-note"
        return [doc for doc in docs if doc.get("type") == wanted_type]
    collectors: dict[str, Callable[[], list[dict[str, Any]]]] = {
        "obsidian": collect_obsidian,
        "experience": collect_experience,
    }
    return collectors[collection]()


def _collector_reindex(collection: str, shadow: str) -> dict[str, Any]:
    docs = _collector_docs(collection)
    count = add_documents(shadow, docs, skip_stale_cleanup=False, force_incremental=False)
    return {"strategy": "collector_source_aware", "source_docs": len(docs), "shadow_points": count}


def _copy_reembed(collection: str, shadow: str, *, batch_size: int, limit: int | None) -> dict[str, Any]:
    store = get_vector_store()
    points = store.get(
        collection,
        limit=limit or 1_000_000,
        with_payload=True,
        with_vectors=False,
        with_documents=True,
    )
    scanned = embedded = skipped_no_doc = skipped_embed = 0
    for start in range(0, len(points), batch_size):
        batch = points[start : start + batch_size]
        ids: list[str] = []
        docs: list[str] = []
        payloads: list[dict[str, Any]] = []
        for point in batch:
            scanned += 1
            doc = point.document or ""
            if not doc.strip():
                skipped_no_doc += 1
                continue
            payload = enrich_payload_for_entry(
                point.payload or {},
                content=doc,
                collection=shadow,
                point_id=point.id,
            )
            payload["source_collection"] = collection
            payload["shadow_of"] = collection
            ids.append(point.id)
            docs.append(doc)
            payloads.append(payload)
        if not ids:
            continue
        vectors = get_embeddings_batch(docs, prefix="passage", use_cache=True)
        good_ids: list[str] = []
        good_docs: list[str] = []
        good_payloads: list[dict[str, Any]] = []
        good_vectors: list[list[float]] = []
        for sid, doc, payload, vec in zip(ids, docs, payloads, vectors, strict=False):
            if not vec:
                skipped_embed += 1
                continue
            good_ids.append(sid)
            good_docs.append(doc)
            good_payloads.append(payload)
            good_vectors.append(vec)
        if good_ids:
            store.upsert(
                shadow, ids=good_ids, vectors=good_vectors, payloads=good_payloads, documents=good_docs
            )
            embedded += len(good_ids)
            print(f"    {collection}->{shadow}: {embedded}/{len(points)} embedded", flush=True)
    return {
        "strategy": "copy_reembed_source_native",
        "scanned": scanned,
        "shadow_points": embedded,
        "skipped_no_document": skipped_no_doc,
        "skipped_embedding": skipped_embed,
    }


def verify_shadow(collection: str, shadow: str) -> dict[str, Any]:
    store = get_vector_store()
    prod_count = store.count(collection)
    shadow_count = store.count(shadow)
    sample = store.get(shadow, limit=200, with_payload=True, with_documents=False)
    missing_contract = 0
    for point in sample:
        payload = point.payload or {}
        if not all(
            payload.get(k)
            for k in ("schema_version", "chunk_version", "tag_policy_version", "content_hash", "tags")
        ):
            missing_contract += 1
    return {
        "production_count": prod_count,
        "shadow_count": shadow_count,
        "sampled": len(sample),
        "sample_missing_contract": missing_contract,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collections", nargs="*", default=DEFAULT_COLLECTIONS)
    parser.add_argument("--suffix", default="shadow_v2")
    parser.add_argument("--reset-shadow", action="store_true")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--limit", type=int, default=None, help="debug limit per collection")
    parser.add_argument("--qdrant-url", default=os.getenv("QDRANT_URL", "http://127.0.0.1:6333"))
    parser.add_argument("--json-out", type=Path, default=Path("logs/source-aware-shadow-reindex.json"))
    args = parser.parse_args()

    client = _client(args.qdrant_url)
    results: dict[str, Any] = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "collections": {},
    }
    args.json_out.parent.mkdir(parents=True, exist_ok=True)

    for collection in args.collections:
        shadow = shadow_name(collection, args.suffix)
        print(f"\n[shadow] {collection} -> {shadow}", flush=True)
        _create_shadow_collection(client, collection, shadow, reset=args.reset_shadow)
        t0 = time.time()
        if collection in COLLECTOR_COLLECTIONS:
            result = _collector_reindex(collection, shadow)
        elif collection in COPY_REEMBED_COLLECTIONS:
            result = _copy_reembed(collection, shadow, batch_size=max(1, args.batch_size), limit=args.limit)
        else:
            result = _copy_reembed(collection, shadow, batch_size=max(1, args.batch_size), limit=args.limit)
        result["elapsed_s"] = round(time.time() - t0, 2)
        result["verify"] = verify_shadow(collection, shadow)
        results["collections"][collection] = result
        args.json_out.write_text(json.dumps(results, indent=2, ensure_ascii=False))
        print(json.dumps({collection: result}, indent=2, ensure_ascii=False), flush=True)

    results["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    args.json_out.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
