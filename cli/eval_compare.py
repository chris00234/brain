#!/opt/homebrew/bin/python3
"""eval_compare.py — compare /recall vs /recall/v2 on eval_set.json.

Runs every test case against both endpoints and reports:
  - hit@1, hit@5 (expected source appears in top-N)
  - content@5 (expected_content substring appears in top-5 content)
  - mean latency per endpoint
  - per-test winner

Usage:
  eval_compare.py                  # basic run: /recall vs /recall/v2 default
  eval_compare.py --hyde           # add &hyde=true to v2
  eval_compare.py --expand         # add &expand=true to v2
  eval_compare.py --hyde --expand  # both
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

EVAL_SET = Path("/Users/chrischo/server/brain/cli/eval_set.json")
SECRET_FILE = Path("/Users/chrischo/.openclaw/credentials/.personal_webhook_secret")
BASE = "http://127.0.0.1:8791"


def _get(path: str, token: str) -> dict:
    req = urllib.request.Request(
        BASE + path,
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e), "results": []}


def _expected_hit(results: list[dict], expected_source: str, expected_content: str) -> tuple[bool, bool, int]:
    """Returns (hit_source_top5, hit_content_top5, hit_at_rank). rank is 1-indexed, 0 if no hit."""
    rank = 0
    hit_source = False
    hit_content = False
    exp_source_lower = (expected_source or "").lower()
    exp_content_lower = (expected_content or "").lower()
    for i, r in enumerate(results[:5], 1):
        path = (r.get("path") or r.get("source") or "").lower()
        title = (r.get("title") or "").lower()
        content = (r.get("content") or "").lower()
        collection = (r.get("collection") or "").lower()
        source_type = (r.get("source_type") or "").lower()
        if exp_source_lower and (
            exp_source_lower in path
            or exp_source_lower in title
            or exp_source_lower == collection
            or exp_source_lower == source_type
        ):
            if rank == 0:
                rank = i
            hit_source = True
        if exp_content_lower and exp_content_lower in content:
            hit_content = True
    return hit_source, hit_content, rank


def run_eval(use_v2: bool, hyde: bool, expand: bool, token: str, cases: list[dict]) -> dict:
    hits_source = 0
    hits_content = 0
    ranks: list[int] = []
    latencies: list[float] = []
    per_test: list[dict] = []

    for case in cases:
        q = case["query"]
        expected_source = case.get("expected_source", "")
        expected_content = case.get("expected_content", "")

        if use_v2:
            params = {"q": q, "n": "5"}
            if hyde:
                params["hyde"] = "true"
            if expand:
                params["expand"] = "true"
            path = "/recall/v2?" + urllib.parse.urlencode(params)
        else:
            path = "/recall?" + urllib.parse.urlencode({"q": q, "n": "5"})

        t0 = time.time()
        payload = _get(path, token)
        dt = (time.time() - t0) * 1000
        latencies.append(dt)

        results = payload.get("results", [])
        hs, hc, rank = _expected_hit(results, expected_source, expected_content)
        if hs:
            hits_source += 1
        if hc:
            hits_content += 1
        if rank > 0:
            ranks.append(rank)

        per_test.append({
            "query": q,
            "hit_source": hs,
            "hit_content": hc,
            "rank": rank,
            "latency_ms": int(dt),
        })

    total = len(cases)
    return {
        "total": total,
        "hit_source_pct": round(100 * hits_source / total, 1) if total else 0,
        "hit_content_pct": round(100 * hits_content / total, 1) if total else 0,
        "mean_rank": round(sum(ranks) / len(ranks), 2) if ranks else 0,
        "mean_latency_ms": round(sum(latencies) / len(latencies), 0) if latencies else 0,
        "per_test": per_test,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare /recall vs /recall/v2")
    parser.add_argument("--hyde", action="store_true")
    parser.add_argument("--expand", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="Only run first N cases (0 = all)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not SECRET_FILE.exists():
        sys.stderr.write(f"no secret file at {SECRET_FILE}\n")
        return 2
    token = SECRET_FILE.read_text().strip()

    cases = json.loads(EVAL_SET.read_text())
    if args.limit > 0:
        cases = cases[:args.limit]

    if not args.json:
        print(f"Running {len(cases)} eval cases against /recall and /recall/v2...")
    baseline = run_eval(use_v2=False, hyde=False, expand=False, token=token, cases=cases)
    v2 = run_eval(use_v2=True, hyde=args.hyde, expand=args.expand, token=token, cases=cases)

    mode = "basic"
    if args.hyde and args.expand:
        mode = "hyde+expand"
    elif args.hyde:
        mode = "hyde"
    elif args.expand:
        mode = "expand"

    report = {
        "cases": len(cases),
        "v2_mode": mode,
        "baseline": {k: v for k, v in baseline.items() if k != "per_test"},
        "v2":       {k: v for k, v in v2.items() if k != "per_test"},
    }

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print("\n" + "=" * 60)
    print(f"Eval comparison — {mode} mode, {len(cases)} cases")
    print("=" * 60)
    for name, r in (("/recall (baseline)", baseline), (f"/recall/v2 ({mode})", v2)):
        print(f"\n{name}")
        print(f"  hit_source@5    : {r['hit_source_pct']:5.1f}%")
        print(f"  hit_content@5   : {r['hit_content_pct']:5.1f}%")
        print(f"  mean rank       : {r['mean_rank']}")
        print(f"  mean latency    : {r['mean_latency_ms']:5.0f} ms")

    ds = v2["hit_source_pct"] - baseline["hit_source_pct"]
    dc = v2["hit_content_pct"] - baseline["hit_content_pct"]
    print("\nDelta (v2 − baseline)")
    print(f"  hit_source@5    : {ds:+.1f} pts")
    print(f"  hit_content@5   : {dc:+.1f} pts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
