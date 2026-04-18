#!/opt/homebrew/bin/python3
"""Tune HNSW ef_search per collection. One-time setup script."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # brain_core/
from datetime import UTC

from http_pool import http_json

CHROMA_API = "http://127.0.0.1:8000/api/v2/tenants/default_tenant/databases/default_database/collections"

# Recommended ef_search per collection based on size + precision needs.
# Tuned 2026-04-15 for /recall/v2 latency budget — canonical/sem_mem/experience
# dropped to hit the 350ms p95 target while stable eval content_hit held at
# baseline 95.7%. The live Chroma metadata was updated via PUT at the same
# time; these values are the source of truth for fresh-machine / restore-from-
# backup paths and the nightly hnsw_tune adaptive job.
SETTINGS = {
    "canonical": 120,  # was 200; 4301 chunks, 200 was overkill
    "knowledge": 100,  # medium
    "experience": 75,  # was 100; 4775 chunks, speed-prioritized
    "context": 100,
    "semantic_memory": 100,  # was 150
    "obsidian": 50,  # large, speed-prioritized
    "notes": 50,
    "messages": 50,
    "calendar": 50,
    "tasks": 50,
    "personal": 75,
}

TUNING_LOG = Path("/Users/chrischo/server/brain/logs/hnsw_tuning.jsonl")

# Target p95 latency per collection type (ms)
TARGETS = {
    "canonical": 150,
    "knowledge": 200,
    "experience": 300,
    "context": 200,
    "semantic_memory": 200,
    "obsidian": 400,  # large corpus
    "notes": 400,
    "messages": 400,
    "calendar": 200,
    "tasks": 200,
    "personal": 300,
}


def get_current_ef_search(col_name: str) -> int:
    """Return the configured ef_search for a collection.

    We can't reliably read live HNSW state from ChromaDB via the v2 API —
    `GET /collections/{name}` isn't exposed for native-mode collections
    consistently. Treat the static SETTINGS dict as the source of truth
    (it's what we pass at collection init).
    """
    return SETTINGS.get(col_name, 100)


def measure_collection_p95(col_name: str) -> float | None:
    """Measure p95 latency. Uses per-collection tracking if available,
    otherwise returns the global p95 (all collections treated the same)."""
    try:
        from metrics_buffer import metrics_buffer as mb

        if hasattr(mb, "per_collection_latency"):
            stats = mb.per_collection_latency(col_name)
            return stats.get("p95") if stats else None
        stats = mb.search_latency_stats() if hasattr(mb, "search_latency_stats") else {}
        return stats.get("p95")
    except Exception:
        return None


def adaptive_tune(dry_run: bool = False) -> dict:
    """Adjust ef_search based on measured latency vs target.

    Rules:
    - p95 > 2x target → reduce ef_search by 25%
    - p95 < 0.5x target → increase ef_search by 25% (more quality)
    - else → no change
    - bounds: [30, 300]
    """
    results: dict = {"checked": 0, "adjusted": [], "no_change": [], "no_data": []}

    cols = get_collections()
    for col_name, target_p95 in TARGETS.items():
        col_id = cols.get(col_name)
        if not col_id:
            continue
        results["checked"] += 1

        current_ef = get_current_ef_search(col_name)

        measured_p95 = measure_collection_p95(col_name)
        # Treat zero / None as "no signal" — tuning on an empty buffer would
        # produce a false "increase ef_search" on every collection.
        if not measured_p95 or measured_p95 <= 0:
            results["no_data"].append(col_name)
            continue

        new_ef = current_ef
        action = "no_change"

        if measured_p95 > 2 * target_p95:
            new_ef = max(30, int(current_ef * 0.75))
            action = "reduced"
        elif measured_p95 < 0.5 * target_p95:
            new_ef = min(300, int(current_ef * 1.25))
            action = "increased"

        if new_ef != current_ef:
            entry = {
                "collection": col_name,
                "current_ef": current_ef,
                "new_ef": new_ef,
                "p95_ms": measured_p95,
                "target_p95": target_p95,
                "action": action,
            }
            if not dry_run:
                if update_collection_hnsw(col_id, new_ef):
                    _log_tuning(entry)
                    results["adjusted"].append(entry)
                else:
                    entry["error"] = "update failed"
                    results["no_change"].append(entry)
            else:
                entry["dry_run"] = True
                results["adjusted"].append(entry)
        else:
            results["no_change"].append({"collection": col_name, "ef": current_ef, "p95_ms": measured_p95})

    return results


def _log_tuning(entry: dict) -> None:
    from datetime import datetime

    TUNING_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry["timestamp"] = datetime.now(UTC).isoformat()
    try:
        with TUNING_LOG.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def get_collections() -> dict:
    """Get {name: id} mapping."""
    try:
        resp = http_json("GET", CHROMA_API)
        if isinstance(resp, list):
            return {c["name"]: c["id"] for c in resp if c.get("name") and c.get("id")}
    except Exception:
        pass
    return {}


def update_collection_hnsw(col_id: str, ef_search: int) -> bool:
    """Persist a new ef_search hint on the collection metadata.

    Note: this does NOT update the live HNSW index — ChromaDB reads
    hnsw:search_ef at collection init time. The update is recorded so
    that the NEXT collection load picks it up (e.g. after a server
    restart or cache eviction). Callers should treat this as an
    advisory write, not a live retuning.
    """
    try:
        http_json(
            "PUT",
            f"{CHROMA_API}/{col_id}",
            payload={
                "new_metadata": {"hnsw:search_ef": ef_search},
            },
        )
        return True
    except Exception as e:
        print(f"  failed to update {col_id}: {e}")
        return False


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Tune HNSW ef_search per collection")
    parser.add_argument("--verify", action="store_true", help="Only verify current settings")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--adaptive", action="store_true", help="Measure actual p95 and adjust ef_search dynamically"
    )
    args = parser.parse_args()

    if args.adaptive:
        result = adaptive_tune(dry_run=args.dry_run)
        print(json.dumps(result, indent=2))
        return 0

    cols = get_collections()
    print(f"Found {len(cols)} collections")

    results = {}
    for name, target_ef in SETTINGS.items():
        col_id = cols.get(name)
        if not col_id:
            print(f"  {name}: not found, skipping")
            continue

        if args.verify or args.dry_run:
            print(f"  {name}: would set ef_search={target_ef}")
            continue

        if update_collection_hnsw(col_id, target_ef):
            print(f"  {name}: ef_search={target_ef} OK")
            results[name] = target_ef

    print(f"\n{len(results)} collections tuned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
