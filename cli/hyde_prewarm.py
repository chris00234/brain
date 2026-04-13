#!/Users/chrischo/server/brain/.venv/bin/python
"""hyde_prewarm.py — Pre-generate HyDE hypothetical answers into the persistent cache.

Reads queries from one or more sources (eval_set, recall-gaps.jsonl) and calls
Jenna to generate a hypothetical for each one, storing it in the SQLite cache
so live /recall/v2?hyde=true calls become <5ms cache hits.

One-time startup cost: ~15-20s per unique query on cache miss. After this
script runs once, the cache is warm for life (it survives restarts).

Usage:
  hyde_prewarm.py                       # default: eval_set.json + recall-gaps
  hyde_prewarm.py --eval-set PATH       # override source
  hyde_prewarm.py --limit N             # only first N queries
  hyde_prewarm.py --skip-cached         # skip queries already in cache (default on)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
from hyde import generate_hypothetical, hyde_cache_stats, _hyde_disk_cache  # noqa: E402

EVAL_SET_DEFAULT = Path("/Users/chrischo/server/brain/cli/eval_set.json")
RECALL_GAPS = Path("/Users/chrischo/server/brain/logs/recall-gaps.jsonl")


def _load_eval_queries(path: Path) -> list[str]:
    try:
        cases = json.loads(path.read_text())
        return [(c.get("query") or "").strip() for c in cases if c.get("query")]
    except Exception:
        return []


def _load_gap_queries(path: Path, max_lines: int = 2000) -> list[str]:
    if not path.exists():
        return []
    queries: list[str] = []
    try:
        for line in path.read_text().splitlines()[-max_lines:]:
            try:
                entry = json.loads(line)
                q = (entry.get("query") or "").strip()
                if q and q not in queries:
                    queries.append(q)
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    return queries


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval-set", type=Path, default=EVAL_SET_DEFAULT)
    ap.add_argument("--recall-gaps", type=Path, default=RECALL_GAPS)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--skip-cached", action="store_true", default=True)
    args = ap.parse_args()

    print(f"hyde_prewarm — starting")
    print(f"  cache stats: {hyde_cache_stats()}")

    # Collect queries from all sources, dedupe
    queries: list[str] = []
    seen: set[str] = set()
    for source_name, loader, path in [
        ("eval_set", _load_eval_queries, args.eval_set),
        ("recall_gaps", _load_gap_queries, args.recall_gaps),
    ]:
        if not path.exists():
            print(f"  skip {source_name}: {path} not found")
            continue
        qs = loader(path)
        new = [q for q in qs if q and q not in seen]
        seen.update(new)
        queries.extend(new)
        print(f"  {source_name}: {len(qs)} loaded, {len(new)} new")

    if args.limit > 0:
        queries = queries[:args.limit]

    print(f"  total unique queries: {len(queries)}")

    t_start = time.time()
    dispatched = 0
    cached_hits = 0
    failed = 0

    for i, q in enumerate(queries, 1):
        if args.skip_cached and _hyde_disk_cache.get(q):
            cached_hits += 1
            continue
        t_q = time.time()
        try:
            reply = generate_hypothetical(q, allow_dispatch=True)
            if reply:
                dispatched += 1
            else:
                failed += 1
        except Exception as e:
            print(f"    [{i}/{len(queries)}] ERROR: {e}", file=sys.stderr)
            failed += 1
            continue
        dt = time.time() - t_q
        elapsed = time.time() - t_start
        if i % 10 == 0 or dispatched + failed < 5:
            print(f"  [{i}/{len(queries)}] done={dispatched} fail={failed} skipped={cached_hits} "
                  f"({dt:.1f}s last, {elapsed:.0f}s elapsed)")

    print(f"\nhyde_prewarm DONE")
    print(f"  dispatched: {dispatched}")
    print(f"  failed:     {failed}")
    print(f"  skipped:    {cached_hits}")
    print(f"  elapsed:    {time.time() - t_start:.0f}s")
    print(f"  final stats: {hyde_cache_stats()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
