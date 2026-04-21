#!/opt/homebrew/bin/python3
"""cli/qdrant_bootstrap.py — create Qdrant collections with production schema.

Phase B1 of the bold-path migration (see
``~/.claude/plans/toasty-snacking-shamir.md``).

Creates 7 target collections mapping from 13 legacy ChromaDB collections:

    canonical          ← canonical + canonical_raptor
    semantic_memory    ← semantic_memory + semantic_contradictions
    experience         ← experience + experience_compressed
    knowledge          ← knowledge + context + patterns
    code               ← code (standalone)
    personal           ← personal (standalone)
    obsidian           ← obsidian (standalone)
    (dropped: healthcheck_probe)

Each collection gets:
- Primary `dense` named vector (1024-d cosine, e5-large-instruct)
- Payload-level `_original_id` / `_document` reserved keys (set by
  QdrantStore at upsert time, no schema support needed).
- int8 scalar quantization with always_ram=true + on_disk float32 for
  rescoring.
- HNSW m=16, ef_construct=128.
- Payload indexes on every field actually filtered by hot paths.

Canonical additionally gets `contextual` and `raptor` named vectors for
the multi-vector schema that the migration will populate from the
folded canonical_raptor collection.

Idempotent: if a collection exists, skip creation but re-apply missing
payload indexes (Qdrant's create_payload_index is already idempotent).

Usage:
    python cli/qdrant_bootstrap.py
    python cli/qdrant_bootstrap.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))

from qdrant_client import QdrantClient  # noqa: E402
from qdrant_client.models import (  # noqa: E402
    Distance,
    HnswConfigDiff,
    PayloadSchemaType,
    ScalarQuantization,
    ScalarQuantizationConfig,
    ScalarType,
    VectorParams,
)

VECTOR_SIZE = 1024  # multilingual-e5-large-instruct

QUANTIZATION = ScalarQuantization(
    scalar=ScalarQuantizationConfig(
        type=ScalarType.INT8,
        quantile=0.99,
        always_ram=True,
    )
)

HNSW = HnswConfigDiff(m=16, ef_construct=128, full_scan_threshold=10000, on_disk=False)


def _vector_params(size: int = VECTOR_SIZE) -> VectorParams:
    return VectorParams(
        size=size,
        distance=Distance.COSINE,
        on_disk=True,  # full fp32 on NVMe for rescoring
    )


# Per-collection schema. Each entry:
#   vectors: dict of {name: VectorParams} for named-vector collections,
#            or a single VectorParams for the canonical unnamed case.
#   payload_indexes: [(field, schema_type)]
SCHEMAS: dict[str, dict] = {
    "canonical": {
        "vectors": {
            "dense": _vector_params(),
            "contextual": _vector_params(),
            "raptor": _vector_params(),
        },
        "payload_indexes": [
            ("agent", PayloadSchemaType.KEYWORD),
            ("type", PayloadSchemaType.KEYWORD),
            ("source", PayloadSchemaType.KEYWORD),
            ("status", PayloadSchemaType.KEYWORD),
            ("category", PayloadSchemaType.KEYWORD),
            ("embed_model_version", PayloadSchemaType.KEYWORD),
            ("superseded_by", PayloadSchemaType.KEYWORD),
            ("supersedes", PayloadSchemaType.KEYWORD),
            ("contextualized", PayloadSchemaType.BOOL),
            ("raptor_level", PayloadSchemaType.INTEGER),
            ("created_at", PayloadSchemaType.DATETIME),
            ("valid_until", PayloadSchemaType.DATETIME),
        ],
    },
    "semantic_memory": {
        "vectors": {"dense": _vector_params()},
        "payload_indexes": [
            ("agent", PayloadSchemaType.KEYWORD),
            ("type", PayloadSchemaType.KEYWORD),
            ("category", PayloadSchemaType.KEYWORD),
            ("kind", PayloadSchemaType.KEYWORD),
            ("memory_class", PayloadSchemaType.KEYWORD),
            ("scope", PayloadSchemaType.KEYWORD),
            ("speaker_entity", PayloadSchemaType.KEYWORD),
            ("source", PayloadSchemaType.KEYWORD),
            ("embed_model_version", PayloadSchemaType.KEYWORD),
            ("superseded_by", PayloadSchemaType.KEYWORD),
            ("supersedes", PayloadSchemaType.KEYWORD),
            ("review_state", PayloadSchemaType.KEYWORD),
            ("confidence", PayloadSchemaType.FLOAT),
            ("trust_score", PayloadSchemaType.FLOAT),
            ("access_count", PayloadSchemaType.INTEGER),
            ("created_at", PayloadSchemaType.DATETIME),
            ("valid_from", PayloadSchemaType.DATETIME),
            ("valid_until", PayloadSchemaType.DATETIME),
            ("last_accessed_at", PayloadSchemaType.DATETIME),
        ],
    },
    "experience": {
        "vectors": {"dense": _vector_params()},
        "payload_indexes": [
            ("agent", PayloadSchemaType.KEYWORD),
            ("type", PayloadSchemaType.KEYWORD),
            ("service", PayloadSchemaType.KEYWORD),
            ("source", PayloadSchemaType.KEYWORD),
            ("memory_class", PayloadSchemaType.KEYWORD),
            ("compressed", PayloadSchemaType.BOOL),
            ("compressed_into", PayloadSchemaType.KEYWORD),
            ("created_at", PayloadSchemaType.DATETIME),
        ],
    },
    "knowledge": {
        "vectors": {"dense": _vector_params()},
        "payload_indexes": [
            ("agent", PayloadSchemaType.KEYWORD),
            ("type", PayloadSchemaType.KEYWORD),
            ("service", PayloadSchemaType.KEYWORD),
            ("source", PayloadSchemaType.KEYWORD),
            ("origin", PayloadSchemaType.KEYWORD),  # knowledge | context | patterns
            ("chunk_id", PayloadSchemaType.KEYWORD),
            ("parent_id", PayloadSchemaType.KEYWORD),
            ("is_parent", PayloadSchemaType.BOOL),
            ("embed_model_version", PayloadSchemaType.KEYWORD),
            ("created_at", PayloadSchemaType.DATETIME),
        ],
    },
    "code": {
        "vectors": {"dense": _vector_params()},
        "payload_indexes": [
            ("repo", PayloadSchemaType.KEYWORD),
            ("language", PayloadSchemaType.KEYWORD),
            ("kind", PayloadSchemaType.KEYWORD),
            ("file_path", PayloadSchemaType.KEYWORD),
            ("function_name", PayloadSchemaType.KEYWORD),
            ("line_start", PayloadSchemaType.INTEGER),
            ("embed_model_version", PayloadSchemaType.KEYWORD),
            ("indexed_at", PayloadSchemaType.DATETIME),
        ],
    },
    "personal": {
        "vectors": {"dense": _vector_params()},
        "payload_indexes": [
            ("type", PayloadSchemaType.KEYWORD),
            ("service", PayloadSchemaType.KEYWORD),
            ("status", PayloadSchemaType.KEYWORD),
            ("source", PayloadSchemaType.KEYWORD),
            ("date", PayloadSchemaType.DATETIME),
            ("event_date", PayloadSchemaType.DATETIME),
            ("modified_at", PayloadSchemaType.DATETIME),
            ("created_at", PayloadSchemaType.DATETIME),
            ("embed_model_version", PayloadSchemaType.KEYWORD),
        ],
    },
    "obsidian": {
        "vectors": {"dense": _vector_params()},
        "payload_indexes": [
            ("type", PayloadSchemaType.KEYWORD),
            ("source", PayloadSchemaType.KEYWORD),
            ("vault_subdir", PayloadSchemaType.KEYWORD),
            ("embed_model_version", PayloadSchemaType.KEYWORD),
            ("created_at", PayloadSchemaType.DATETIME),
            ("mtime", PayloadSchemaType.DATETIME),
        ],
    },
}


def bootstrap(*, dry_run: bool = False, url: str = "http://127.0.0.1:6333") -> int:
    client = QdrantClient(url=url, timeout=30)

    existing = {c.name for c in (client.get_collections().collections or [])}
    if existing:
        print(f"already present: {sorted(existing)}")

    for name, spec in SCHEMAS.items():
        vectors = spec["vectors"]
        indexes = spec["payload_indexes"]

        if name in existing:
            print(f"\n[skip] {name} — already exists")
        elif dry_run:
            vec_names = list(vectors) if isinstance(vectors, dict) else ["<single>"]
            print(f"\n[dry-run] would create {name} (vectors={vec_names}, indexes={len(indexes)})")
        else:
            print(f"\ncreating {name}...")
            client.create_collection(
                collection_name=name,
                vectors_config=vectors,
                quantization_config=QUANTIZATION,
                hnsw_config=HNSW,
            )
            print(f"  ok: {name}")

        if dry_run:
            continue
        # Payload indexes — idempotent; re-run is safe.
        for field, schema in indexes:
            try:
                client.create_payload_index(
                    collection_name=name,
                    field_name=field,
                    field_schema=schema,
                )
            except Exception as exc:
                # Qdrant returns "already indexed" as an error in some client
                # versions; tolerate silently, surface anything else.
                if "already" in str(exc).lower() or "conflict" in str(exc).lower():
                    continue
                print(f"  WARN: index {name}.{field} failed: {exc}")

    # Smoke a tiny query against one collection to verify named-vector +
    # quantization accept reads. Uses qdrant-client 1.17+ `query_points`.
    if not dry_run:
        from qdrant_client.models import PointIdsList, PointStruct

        probe_collection = "semantic_memory"
        probe_id = "00000000-0000-0000-0000-000000000001"
        probe_vec = [0.1] * VECTOR_SIZE
        try:
            client.upsert(
                collection_name=probe_collection,
                points=[
                    PointStruct(
                        id=probe_id,
                        vector={"dense": probe_vec},
                        payload={"_bootstrap_probe": True},
                    )
                ],
                wait=True,
            )
            resp = client.query_points(
                collection_name=probe_collection,
                query=probe_vec,
                using="dense",
                limit=1,
            )
            print(f"\nprobe query on {probe_collection}: {len(resp.points)} hit(s)")
            client.delete(
                collection_name=probe_collection,
                points_selector=PointIdsList(points=[probe_id]),
                wait=True,
            )
        except Exception as exc:
            print(f"  WARN: bootstrap probe failed: {exc}")
            return 1

    print("\ndone.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--url", default="http://127.0.0.1:6333")
    args = parser.parse_args()
    return bootstrap(dry_run=args.dry_run, url=args.url)


if __name__ == "__main__":
    sys.exit(main())
