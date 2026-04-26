#!/usr/bin/env python3
"""Audit canonical data for stale current-state infrastructure claims."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRAIN_CORE = ROOT / "brain_core"
if str(BRAIN_CORE) not in sys.path:
    sys.path.insert(0, str(BRAIN_CORE))

from stale_current_truth import (  # noqa: E402
    DEFAULT_DECOMMISSIONED_TERMS_PATH,
    build_atoms_report,
    build_canonical_report,
    build_vector_report,
)


def print_text(report: dict) -> None:
    print(f"Stale current-truth audit: {'PASS' if report['passed'] else 'FAIL'}")
    print(f"Files scanned: {report['files_scanned']}")
    print(f"Historical mentions allowed: {report['historical_mentions_allowed']}")
    if report["blockers"]:
        print("\nBlockers:")
        for blocker in report["blockers"]:
            print(
                f"- {blocker['file']}:{blocker['line']} {blocker['term']} -> {blocker['replaced_by']}: {blocker['text']}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit canonical data for stale current-state claims")
    parser.add_argument("--knowledge-root", type=Path, default=ROOT.parent / "knowledge")
    parser.add_argument("--config", type=Path, default=DEFAULT_DECOMMISSIONED_TERMS_PATH)
    parser.add_argument("--scan-vector", action="store_true", help="Also scan Qdrant vector collections")
    parser.add_argument("--scan-atoms", action="store_true", help="Also scan the SQLite atoms truth layer")
    parser.add_argument(
        "--collections",
        default="semantic_memory,canonical,experience,knowledge,personal,obsidian",
        help="Comma-separated Qdrant collections for --scan-vector",
    )
    parser.add_argument(
        "--apply", action="store_true", help="Mark stale vector points obsolete instead of only reporting"
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--fail-on-blockers", action="store_true")
    args = parser.parse_args()

    report = build_canonical_report(args.knowledge_root, config_path=args.config)
    if args.scan_vector:
        collections = tuple(item.strip() for item in args.collections.split(",") if item.strip())
        report["vector"] = build_vector_report(
            collections=collections, config_path=args.config, apply=args.apply
        )
        report["passed"] = bool(report.get("passed")) and bool(report["vector"].get("passed"))
        report["blocker_count"] = int(report.get("blocker_count") or 0) + int(
            report["vector"].get("blocker_count") or 0
        )
    if args.scan_atoms:
        report["atoms"] = build_atoms_report(config_path=args.config, apply=args.apply)
        report["passed"] = bool(report.get("passed")) and bool(report["atoms"].get("passed"))
        report["blocker_count"] = int(report.get("blocker_count") or 0) + int(
            report["atoms"].get("blocker_count") or 0
        )
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print_text(report)
    if args.fail_on_blockers and int(report.get("blocker_count") or 0) > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
