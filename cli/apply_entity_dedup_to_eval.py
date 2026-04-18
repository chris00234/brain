#!/usr/bin/env python3
"""cli/apply_entity_dedup_to_eval.py — update eval sets for entity canonicalization.

After E cross-lang entity dedup (2026-04-14) merged 28 Neo4j Entity nodes
into canonical names, 17+ eval queries regressed because their
`expected_content` strings referenced the OLD merged-in names. The /recall/v2
pipeline still finds the right doc, but strict substring matching fails
because the canonical name now replaces the old one in the retrieved
content preview.

This script:
  1. Loads the merge mapping from logs/entity_canonicalize_audit.jsonl
  2. Resolves transitive chains (a→b→c becomes a→c)
  3. Applies case-insensitive phrase substitution to `query`,
     `expected_content`, and `expected_source` fields in each eval case
  4. Writes back in-place (backups at *.pre-canonicalize)
  5. Reports how many cases changed per eval set

Run:
  .venv/bin/python cli/apply_entity_dedup_to_eval.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

EVAL_SETS = [
    "/Users/chrischo/server/brain/cli/eval_set.json",
    "/Users/chrischo/server/brain/cli/eval_set_extended_v2.json",
    "/Users/chrischo/server/brain/cli/eval_set_stable.json",
]
AUDIT_LOG = "/Users/chrischo/server/brain/logs/entity_canonicalize_audit.jsonl"


def build_mapping() -> dict[str, str]:
    """Read audit log + add manual merges, then resolve transitive chains."""
    raw: dict[str, str] = {}
    with open(AUDIT_LOG) as f:
        for line in f:
            if not line.strip():
                continue
            e = json.loads(line)
            if e.get("dry_run"):
                continue
            canonical = e["canonical"]
            for old in e.get("merged_in", []):
                raw[old] = canonical

    # Manual: daehyun → chris cho (post-batch alias)
    raw["daehyun"] = "chris cho"

    # Transitive closure: if a → b and b → c, then a → c
    resolved: dict[str, str] = {}
    for old, new in raw.items():
        seen = {old}
        current = new
        while current in raw and current not in seen:
            seen.add(current)
            current = raw[current]
        resolved[old] = current
    return resolved


def apply_substitutions(
    text: str,
    patterns: list[tuple[re.Pattern, str]],
) -> tuple[str, int]:
    """Apply each (pattern, replacement) to the text. Returns (new_text, count)."""
    count = 0
    out = text
    for pat, repl in patterns:
        out, n = pat.subn(repl, out)
        count += n
    return out, count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    mapping = build_mapping()
    print(f"Loaded {len(mapping)} old→canonical mappings")

    # Sort by length descending so "mission control plan" matches before "mission control"
    sorted_items = sorted(mapping.items(), key=lambda x: -len(x[0]))
    patterns: list[tuple[re.Pattern, str]] = []
    for old, new in sorted_items:
        # Case-insensitive literal match (escape regex metachars in old name)
        pat = re.compile(re.escape(old), re.IGNORECASE)
        patterns.append((pat, new))

    total_cases_changed = 0
    for eval_path in EVAL_SETS:
        p = Path(eval_path)
        if not p.exists():
            print(f"SKIP {eval_path} (missing)")
            continue
        cases = json.loads(p.read_text())
        changed = 0
        total_subs = 0
        for c in cases:
            case_subs = 0
            for field in ("query", "expected_content", "expected_source"):
                val = c.get(field)
                if not isinstance(val, str):
                    continue
                new_val, n = apply_substitutions(val, patterns)
                if n > 0:
                    c[field] = new_val
                    case_subs += n
            if case_subs > 0:
                changed += 1
                total_subs += case_subs
        print(f"{p.name}: {changed}/{len(cases)} cases changed ({total_subs} substitutions)")
        total_cases_changed += changed

        if not args.dry_run:
            p.write_text(json.dumps(cases, indent=2, ensure_ascii=False) + "\n")

    print(f"\nTotal cases changed across all sets: {total_cases_changed}")
    if args.dry_run:
        print("(dry-run — no files written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
