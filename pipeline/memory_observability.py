from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from common import ROOT, iter_note_paths, parse_markdown_frontmatter

CANONICAL_ROOT = ROOT / "canonical"
DISTILLED_ROOT = ROOT / "distilled"
RAW_ROOT = ROOT / "raw" / "inbox"
REVIEW_QUEUE = ROOT / "reports" / "review-queue"
REJECTED_ROOT = REVIEW_QUEUE / "rejected"


def read_notes(root: Path, allow_missing: bool = True) -> list[tuple[Path, dict[str, Any], str]]:
    """Walk note folder, skip files with bad/missing frontmatter."""
    if not root.exists():
        return []
    import logging

    log = logging.getLogger("brain.memory_observability.read_notes")
    notes: list[tuple[Path, dict[str, Any], str]] = []
    for path in iter_note_paths(root):
        try:
            metadata, body = parse_markdown_frontmatter(path)
        except Exception as e:
            log.debug("skipping malformed note %s: %s", path, e)
            continue
        notes.append((path, metadata, body))
    return notes


def build_domain_index(notes: list[tuple[Path, dict[str, Any], str]]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for _path, metadata, _body in notes:
        counts[str(metadata.get("domain", "unknown"))] += 1
    return dict(sorted(counts.items(), key=lambda item: item[0]))


def stale_ratio(notes: list[tuple[Path, dict[str, Any], str]], *, field: str = "updated_at") -> float:
    stale = 0
    for _path, metadata, _body in notes:
        value = metadata.get(field)
        if not value:
            continue
        try:
            when = datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            continue
        if (datetime.now(UTC) - when).days > 180:
            stale += 1
    return stale / max(len(notes), 1)


def low_confidence_count(notes: list[tuple[Path, dict[str, Any], str]], threshold: float) -> int:
    return sum(1 for _path, metadata, _body in notes if float(metadata.get("confidence", 1) or 1) < threshold)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate health metrics for memory pipeline")
    parser.add_argument("--review-queue", type=Path, default=REVIEW_QUEUE)
    parser.add_argument("--out", type=Path, default=REVIEW_QUEUE / "observability_report.json")
    parser.add_argument("--low-confidence-threshold", type=float, default=0.7)
    args = parser.parse_args()

    canonical = read_notes(CANONICAL_ROOT)
    distilled = read_notes(DISTILLED_ROOT)
    proposals = read_notes(args.review_queue)
    rejected = read_notes(
        REJECTED_ROOT,
    )

    raw_count = len(list((RAW_ROOT).glob("raw_*.json"))) if RAW_ROOT.exists() else 0

    pending_proposals = [
        item
        for item in proposals
        if item[1].get("type") == "canonical" and item[1].get("review_state") == "proposed"
    ]
    rejected_count = len([item for item in rejected if item[1].get("type") == "canonical"])

    payload: dict[str, Any] = {
        "status": "ok",
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "raw_inbox_count": raw_count,
        "distilled_count": len(distilled),
        "canonical_count": len(canonical),
        "proposal_queue_total": len(proposals),
        "proposal_queue_pending": len(pending_proposals),
        "proposal_queue_rejected": rejected_count,
        "canonical_by_domain": build_domain_index(canonical),
        "distilled_by_domain": build_domain_index(distilled),
        "proposed_by_domain": build_domain_index(pending_proposals),
        "canonical_stale_ratio": stale_ratio(canonical),
        "distilled_stale_ratio": stale_ratio(distilled, field="updated_at"),
        "low_confidence_distilled": low_confidence_count(distilled, args.low_confidence_threshold),
        "low_confidence_proposals": low_confidence_count(pending_proposals, args.low_confidence_threshold),
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
