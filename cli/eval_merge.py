#!/Users/chrischo/server/brain/.venv/bin/python
"""eval_merge.py — Merge validated eval candidates into eval_set.json.

Backs up the existing eval_set.json, appends new validated candidates (after
post-merge dedup), writes the result.

Usage:
  eval_merge.py [--input PATH] [--eval-set PATH]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_INPUT = Path("/tmp/brain_eval_validated.jsonl")
DEFAULT_EVAL_SET = Path("/Users/chrischo/server/brain/cli/eval_set.json")


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge validated candidates into eval_set.json")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--eval-set", type=Path, default=DEFAULT_EVAL_SET)
    args = parser.parse_args()

    if not args.input.exists():
        print(f"FATAL: validated input missing at {args.input}", file=sys.stderr)
        return 2
    if not args.eval_set.exists():
        print(f"FATAL: eval_set missing at {args.eval_set}", file=sys.stderr)
        return 2

    existing = json.loads(args.eval_set.read_text())
    if not isinstance(existing, list):
        print(f"FATAL: eval_set is not a list", file=sys.stderr)
        return 2
    print(f"existing eval_set entries: {len(existing)}")

    new_candidates = []
    for line in args.input.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            new_candidates.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    print(f"new validated candidates: {len(new_candidates)}")

    seen: set[str] = set()
    merged: list[dict] = []
    for entry in existing + new_candidates:
        q = (entry.get("query") or "").strip().lower()
        if not q or q in seen:
            continue
        seen.add(q)
        merged.append(entry)

    added = len(merged) - len(existing)
    print(f"after post-merge dedup: {len(merged)} (added {added} unique)")

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = args.eval_set.with_suffix(f".json.bak.{ts}")
    shutil.copy2(args.eval_set, backup_path)
    print(f"backup: {backup_path}")

    args.eval_set.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {len(merged)} entries to {args.eval_set}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
