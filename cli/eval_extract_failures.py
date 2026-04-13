#!/Users/chrischo/server/brain/.venv/bin/python
"""eval_extract_failures.py — Pull the queries that currently fail content@5.

Runs eval_compare's run_eval() directly so we can access the per_test list,
groups failing queries by expected_source, and writes a JSON bundle ready for
corpus gap-filling.

Output: /tmp/eval_failures.json with shape:
  {
    "total": 595,
    "failing": 168,
    "clusters": {
      "canonical/decisions/foo.md": [
        {"query": "...", "expected_content": "...", "hit_source": true},
        ...
      ],
      ...
    }
  }
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from eval_compare import run_eval, SECRET_FILE, DEFAULT_EVAL_SET  # noqa: E402


def main() -> int:
    eval_set_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_EVAL_SET
    cases = json.loads(eval_set_path.read_text())
    token = SECRET_FILE.read_text().strip()

    print(f"running eval on {eval_set_path.name} ({len(cases)} cases)...")
    report = run_eval(use_v2=True, hyde=False, expand=False, token=token, cases=cases)
    print(f"  source={report['hit_source_pct']}%  content={report['hit_content_pct']}%  "
          f"loose={report.get('hit_content_loose_pct', 0)}%  lat={report['mean_latency_ms']}ms")

    failing: list[dict] = []
    for i, t in enumerate(report["per_test"]):
        if not t.get("hit_content"):
            case = cases[i]
            failing.append({
                "query": t["query"],
                "expected_source": case.get("expected_source", ""),
                "expected_content": case.get("expected_content", ""),
                "hit_source": t.get("hit_source", False),
                "hit_content_loose": t.get("hit_content_loose", False),
                "rank": t.get("rank", 0),
            })

    # Group by expected_source
    clusters: dict[str, list[dict]] = defaultdict(list)
    no_source: list[dict] = []
    for f in failing:
        src = f["expected_source"]
        if src:
            clusters[src].append(f)
        else:
            no_source.append(f)

    # Sort by cluster size (bigger = more impactful)
    sorted_clusters = sorted(clusters.items(), key=lambda kv: len(kv[1]), reverse=True)

    out = {
        "total": report["total"],
        "failing": len(failing),
        "failing_source_recovered": sum(1 for f in failing if f["hit_source"]),
        "failing_loose_recovered": sum(1 for f in failing if f["hit_content_loose"]),
        "clusters": {src: items for src, items in sorted_clusters},
        "no_source": no_source,
    }
    Path("/tmp/eval_failures.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))

    print(f"\nFailing: {len(failing)}/{report['total']}  ({100*len(failing)/report['total']:.1f}%)")
    print(f"  of those, source IS in top-5: {out['failing_source_recovered']}")
    print(f"  of those, loose content recovers: {out['failing_loose_recovered']}")
    print(f"\nTop 20 expected_source clusters (by fail count):")
    for i, (src, items) in enumerate(sorted_clusters[:20], 1):
        print(f"  {i:2d}. [{len(items):2d}]  {src[:70]}")
    if no_source:
        print(f"\n  [{len(no_source)}] queries with no expected_source")

    print(f"\nWrote /tmp/eval_failures.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
