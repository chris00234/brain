#!/usr/bin/env python3
"""Failing CLI gate for live Qdrant v2 entry-contract coverage."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))

from entry_contract_audit import audit_collections  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collections", nargs="*", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = audit_collections(args.collections, limit=args.limit)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif result["missing_points"]:
        print(
            f"Entry contract missing on {result['missing_points']}/{result['scanned']} "
            f"sampled points ({result['missing_pct']}%)"
        )
        for col in result["collections"]:
            if col["missing_points"]:
                print(f"  {col['collection']}: {col['missing_points']}/{col['scanned']} missing")
    else:
        print(f"OK: v2 entry contract present on {result['scanned']} sampled points")
    return 1 if result["missing_points"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
