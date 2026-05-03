#!/usr/bin/env python3
"""Backfill Brain v2 entry-contract metadata onto existing Qdrant points.

Default is dry-run. Use --apply to patch payloads and populate the SQLite entry
manifest. This is metadata-only: it does not re-embed or re-chunk content.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

BRAIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))

from entry_manifest import record_vector_entries  # noqa: E402
from source_policy import enrich_payload_for_entry  # noqa: E402
from vector_store import get_vector_store  # noqa: E402

DEFAULT_COLLECTIONS = [
    "canonical",
    "distilled",
    "semantic_memory",
    "experience",
    "knowledge",
    "code",
    "personal",
    "obsidian",
]

CONTRACT_KEYS = {
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
}


def _payload_patch(current: dict[str, Any], enriched: dict[str, Any]) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    for key, value in enriched.items():
        if key.startswith("_"):
            continue
        if (key in CONTRACT_KEYS or key in {"document_id", "source_document_id"}) and current.get(
            key
        ) != value:
            patch[key] = value
    return patch


def _iter_points(collection: str, *, limit: int | None) -> list:
    store = get_vector_store()
    return store.get(
        collection,
        limit=limit or 1_000_000,
        with_payload=True,
        with_vectors=False,
        with_documents=True,
    )


def backfill_collection(collection: str, *, apply: bool, limit: int | None, throttle_s: float) -> dict:
    store = get_vector_store()
    points = _iter_points(collection, limit=limit)
    scanned = patched = manifest = failed = already_ok = 0
    missing_before: dict[str, int] = {key: 0 for key in sorted(CONTRACT_KEYS)}

    for point in points:
        scanned += 1
        current = point.payload or {}
        for key in missing_before:
            if key not in current or current.get(key) in (None, "", [], {}):
                missing_before[key] += 1
        enriched = enrich_payload_for_entry(
            current,
            content=point.document or "",
            collection=collection,
            point_id=point.id,
        )
        patch = _payload_patch(current, enriched)
        if not patch:
            already_ok += 1
            if apply:
                record_vector_entries(
                    collection=collection,
                    ids=[point.id],
                    payloads=[enriched],
                    documents=[point.document or ""],
                )
                manifest += 1
            continue
        if not apply:
            continue
        ok = store.update_payload(collection, ids=[point.id], patch=patch)
        if ok:
            patched += 1
            record_vector_entries(
                collection=collection,
                ids=[point.id],
                payloads=[enriched],
                documents=[point.document or ""],
            )
            manifest += 1
            if throttle_s:
                time.sleep(throttle_s)
        else:
            failed += 1

    return {
        "collection": collection,
        "mode": "apply" if apply else "dry-run",
        "scanned": scanned,
        "already_ok": already_ok,
        "would_patch": 0 if apply else scanned - already_ok,
        "patched": patched,
        "manifest_rows_attempted": manifest,
        "failed": failed,
        "missing_before": {k: v for k, v in missing_before.items() if v},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collections", nargs="*", default=DEFAULT_COLLECTIONS)
    parser.add_argument("--apply", action="store_true", help="patch Qdrant payloads and write manifest")
    parser.add_argument("--limit", type=int, default=None, help="limit points per collection")
    parser.add_argument("--throttle-s", type=float, default=0.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    results = []
    for collection in args.collections:
        result = backfill_collection(
            collection,
            apply=args.apply,
            limit=args.limit,
            throttle_s=max(0.0, args.throttle_s),
        )
        results.append(result)
        if not args.json:
            print(
                f"{collection}: scanned={result['scanned']} "
                f"already_ok={result['already_ok']} "
                f"would_patch={result['would_patch']} patched={result['patched']} failed={result['failed']}"
            )
    if args.json:
        print(json.dumps({"results": results}, indent=2, ensure_ascii=False))
    return 1 if any(r["failed"] for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
