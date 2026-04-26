#!/Users/chrischo/server/brain/.venv/bin/python
"""eval_split.py — deterministic 80/20 train/holdout split for eval_set.json.

Uses a fixed seed so the split is reproducible. Writes:
  - cli/eval_set_train.json     (80%)
  - cli/eval_holdout.json       (20%)

Keeps the original eval_set.json untouched. The sweep driver points at
eval_set_train.json for tuning; the final verification uses eval_holdout.json
to guard against overfit to the tuning set.

Usage:
  eval_split.py                  # re-split using defaults
  eval_split.py --seed 42        # override seed
  eval_split.py --ratio 0.8      # override train fraction
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

EVAL_SET = Path("/Users/chrischo/server/brain/cli/eval_set.json")
TRAIN = Path("/Users/chrischo/server/brain/cli/eval_set_train.json")
HOLDOUT = Path("/Users/chrischo/server/brain/cli/eval_holdout.json")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--ratio", type=float, default=0.8, help="Fraction that goes into train")
    ap.add_argument("--input", type=Path, default=EVAL_SET)
    ap.add_argument("--train", type=Path, default=TRAIN)
    ap.add_argument("--holdout", type=Path, default=HOLDOUT)
    args = ap.parse_args()

    if not args.input.exists():
        print(f"FATAL: {args.input} not found", file=sys.stderr)
        return 2

    cases = json.loads(args.input.read_text())
    if not isinstance(cases, list):
        print("FATAL: eval_set is not a list", file=sys.stderr)
        return 2

    n = len(cases)
    if n < 20:
        print(f"FATAL: eval_set has only {n} entries — too small to split", file=sys.stderr)
        return 2

    rng = random.Random(args.seed)
    indices = list(range(n))
    rng.shuffle(indices)
    cut = int(n * args.ratio)
    train_idx = set(indices[:cut])

    train = [cases[i] for i in range(n) if i in train_idx]
    holdout = [cases[i] for i in range(n) if i not in train_idx]

    args.train.write_text(json.dumps(train, indent=2, ensure_ascii=False) + "\n")
    args.holdout.write_text(json.dumps(holdout, indent=2, ensure_ascii=False) + "\n")

    print(f"input:   {args.input}  ({n} entries)")
    print(f"seed:    {args.seed}")
    print(f"ratio:   {args.ratio}")
    print(f"train:   {args.train}  ({len(train)} entries)")
    print(f"holdout: {args.holdout}  ({len(holdout)} entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
