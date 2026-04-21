#!/opt/homebrew/bin/python3
"""Recommend HNSW ef_search per collection based on measured p95 latency.

Qdrant keeps HNSW `m` / `ef_construct` at the collection level (set at
bootstrap via `HnswConfigDiff`) and `ef_search` is a per-query knob
(`SearchParams(hnsw_ef=...)`). There is no live write-back equivalent to
ChromaDB's `hnsw:search_ef` metadata PUT, so this module now emits
recommendations to `logs/hnsw_tuning.jsonl` and leaves runtime tuning
to the search path (which reads `SETTINGS` directly).
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # brain_core/

SETTINGS = {
    "canonical": 120,
    "knowledge": 100,
    "experience": 75,
    "semantic_memory": 100,
    "obsidian": 50,
    "personal": 75,
    "code": 100,
}

TUNING_LOG = Path("/Users/chrischo/server/brain/logs/hnsw_tuning.jsonl")

TARGETS = {
    "canonical": 150,
    "knowledge": 200,
    "experience": 300,
    "semantic_memory": 200,
    "obsidian": 400,
    "personal": 300,
    "code": 250,
}


def get_current_ef_search(col_name: str) -> int:
    """Return the configured ef_search for a collection."""
    return SETTINGS.get(col_name, 100)


def measure_collection_p95(col_name: str) -> float | None:
    """Measure p95 latency via metrics_buffer if available."""
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
    """Emit ef_search recommendations based on measured latency vs target.

    Rules:
    - p95 > 2x target → recommend ef_search -25%
    - p95 < 0.5x target → recommend ef_search +25%
    - bounds: [30, 300]

    In Qdrant, applying the recommendation is a search-path concern (pass
    `SearchParams(hnsw_ef=N)` on each query). This function logs the
    recommendation; operators update `SETTINGS` when a trend persists.
    """
    results: dict = {"checked": 0, "adjusted": [], "no_change": [], "no_data": []}

    for col_name, target_p95 in TARGETS.items():
        results["checked"] += 1
        current_ef = get_current_ef_search(col_name)
        measured_p95 = measure_collection_p95(col_name)
        if not measured_p95 or measured_p95 <= 0:
            results["no_data"].append(col_name)
            continue

        new_ef = current_ef
        action = "no_change"
        if measured_p95 > 2 * target_p95:
            new_ef = max(30, int(current_ef * 0.75))
            action = "reduce"
        elif measured_p95 < 0.5 * target_p95:
            new_ef = min(300, int(current_ef * 1.25))
            action = "increase"

        entry = {
            "collection": col_name,
            "current_ef": current_ef,
            "recommended_ef": new_ef,
            "p95_ms": measured_p95,
            "target_p95": target_p95,
            "action": action,
        }
        if new_ef != current_ef:
            if not dry_run:
                _log_tuning(entry)
            results["adjusted"].append(entry)
        else:
            results["no_change"].append(entry)

    return results


def _log_tuning(entry: dict) -> None:
    TUNING_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry["timestamp"] = datetime.now(UTC).isoformat()
    try:
        with TUNING_LOG.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Recommend HNSW ef_search per collection")
    parser.add_argument("--verify", action="store_true", help="Only print current settings")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--adaptive", action="store_true", help="Measure p95 and emit recommendations")
    args = parser.parse_args()

    if args.adaptive:
        result = adaptive_tune(dry_run=args.dry_run)
        print(json.dumps(result, indent=2))
        return 0

    print("Current ef_search settings:")
    for name, ef in SETTINGS.items():
        print(f"  {name}: {ef}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
