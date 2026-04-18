from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from common import ROOT, dump_json, read_all_notes

STALE_DAYS = 180
LOW_CONFIDENCE_THRESHOLD = 0.75


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate governance reports for memory notes.")
    parser.add_argument("--root", type=Path, default=ROOT)
    return parser.parse_args()


def age_in_days(value: str | None) -> int | None:
    """Returns age in days, or None if value is missing/malformed."""
    if not value:
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        return int((datetime.now(UTC) - timestamp).days)
    except (ValueError, TypeError):
        return None


def main() -> int:
    args = parse_args()
    notes = read_all_notes(args.root)
    note_ids = {note["metadata"]["id"] for note in notes}

    stale = []
    low_confidence = []
    duplicates = []
    conflicts = []
    orphans = []

    active_canonical = defaultdict(list)
    for note in notes:
        metadata = note["metadata"]
        age = age_in_days(metadata.get("updated_at"))
        if age is not None and age > STALE_DAYS:
            stale.append({"id": metadata["id"], "path": note["path"]})
        conf = float(metadata.get("confidence", 1.0) or 1.0)
        if conf < LOW_CONFIDENCE_THRESHOLD:
            low_confidence.append({"id": metadata["id"], "confidence": conf})
        if metadata["type"] == "canonical" and metadata["status"] == "active":
            active_canonical[(metadata["domain"], metadata["subtype"], metadata["title"])].append(
                metadata["id"]
            )
        for relation in metadata.get("relations", []):
            if relation["target"] not in note_ids:
                orphans.append({"id": metadata["id"], "target": relation["target"]})

    for key, ids in active_canonical.items():
        if len(ids) > 1:
            conflicts.append({"key": key, "ids": ids})
        if len(set(ids)) != len(ids):
            duplicates.append({"key": key, "ids": ids})

    dump_json(args.root / "reports" / "stale" / "stale.json", stale)
    dump_json(args.root / "reports" / "duplicates" / "duplicates.json", duplicates)
    dump_json(args.root / "reports" / "conflicts" / "conflicts.json", conflicts)
    dump_json(
        args.root / "reports" / "review-queue" / "review-queue.json",
        {"low_confidence": low_confidence, "orphan_relations": orphans},
    )
    print(
        json.dumps(
            {
                "status": "ok",
                "stale": len(stale),
                "duplicates": len(duplicates),
                "conflicts": len(conflicts),
                "low_confidence": len(low_confidence),
                "orphan_relations": len(orphans),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
