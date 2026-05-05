"""Audit Qdrant payloads for the Brain v2 entry contract.

This is the runtime counterpart to ``cli/audit_qdrant_writes.py``:
the write audit prevents new raw Qdrant writes from bypassing the vector-store
boundary, while this audit samples live Qdrant payloads and verifies the
required contract fields are actually present.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

DEFAULT_COLLECTIONS = (
    "canonical",
    "distilled",
    "semantic_memory",
    "experience",
    "knowledge",
    "code",
    "personal",
    "obsidian",
)

REQUIRED_ENTRY_KEYS = (
    "schema_version",
    "entry_schema_version",
    "chunk_version",
    "chunk_policy_version",
    "tag_policy_version",
    "content_hash",
    "source_kind",
    "source_type",
    "chunk_strategy",
    "semantic_chunk_candidate",
    "tags",
    "context_tags",
    "vector_collection",
    "vector_point_id",
)


def _is_missing(value: Any) -> bool:
    return value in (None, "", [], {})


def missing_contract_keys(payload: dict[str, Any] | None) -> list[str]:
    data = payload or {}
    return [key for key in REQUIRED_ENTRY_KEYS if key not in data or _is_missing(data.get(key))]


def audit_collection(collection: str, *, limit: int) -> dict[str, Any]:
    from vector_store import get_vector_store

    store = get_vector_store()
    points = store.get(collection, limit=limit, with_payload=True, with_vectors=False, with_documents=False)
    missing_by_key: Counter[str] = Counter()
    missing_points = 0
    examples: list[dict[str, Any]] = []

    for point in points:
        missing = missing_contract_keys(point.payload or {})
        if not missing:
            continue
        missing_points += 1
        missing_by_key.update(missing)
        if len(examples) < 5:
            examples.append({"id": point.id, "missing": missing})

    scanned = len(points)
    return {
        "collection": collection,
        "scanned": scanned,
        "missing_points": missing_points,
        "missing_pct": round((missing_points / scanned) * 100.0, 3) if scanned else 0.0,
        "missing_by_key": dict(sorted(missing_by_key.items())),
        "examples": examples,
    }


def audit_collections(collections: list[str] | None = None, *, limit: int | None = None) -> dict[str, Any]:
    sample_limit = limit or int(os.getenv("BRAIN_ENTRY_CONTRACT_AUDIT_LIMIT", "1000"))
    selected = collections or list(DEFAULT_COLLECTIONS)
    results = [audit_collection(collection, limit=sample_limit) for collection in selected]
    scanned = sum(r["scanned"] for r in results)
    missing = sum(r["missing_points"] for r in results)
    return {
        "status": "ok" if missing == 0 else "breached",
        "sample_limit": sample_limit,
        "collections": results,
        "scanned": scanned,
        "missing_points": missing,
        "missing_pct": round((missing / scanned) * 100.0, 3) if scanned else 0.0,
    }


def run() -> dict[str, Any]:
    return audit_collections()


if __name__ == "__main__":
    sys.stdout.write(json.dumps(run(), indent=2, ensure_ascii=False) + "\n")
