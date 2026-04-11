#!/opt/homebrew/bin/python3
"""load_test.py — lightweight concurrent load test for brain API.

Uses stdlib only (asyncio + urllib) so it runs inside any Python 3.10+
without extra deps. Targets /recall (baseline) and /recall/v2 (default mode —
rerank + time decay, no HyDE to keep latency honest).

Pass criteria (10-collection fan-out, single-user system):
  - ≥10 requests/sec sustained
  - p95 latency < 500ms (for reads that don't hit HyDE)

Usage:
  load_test.py --duration 30 --concurrency 50
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

BASE = "http://127.0.0.1:8791"
SECRET = Path("/Users/chrischo/.openclaw/credentials/.personal_webhook_secret").read_text().strip()

QUERIES = [
    "openclaw gateway",
    "brain server fastapi",
    "chromadb collection",
    "jenna agent",
    "personal ingest",
    "self learning memory",
    "docker nginx",
    "cloudflare tunnel",
    "frontend vite react",
    "conventional commits",
]


def _http_get(path: str) -> tuple[int, float]:
    """Return (status, latency_ms). status=0 means connection error (excluded from stats)."""
    req = urllib.request.Request(
        BASE + path,
        headers={"Authorization": f"Bearer {SECRET}"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()  # drain body
            return resp.status, (time.time() - t0) * 1000
    except Exception:
        return 0, (time.time() - t0) * 1000


async def worker(endpoint_template: str, end_at: float, results: list, loop, executor):
    while time.time() < end_at:
        q = random.choice(QUERIES)
        path = endpoint_template.format(q=urllib_quote(q))
        status, latency = await loop.run_in_executor(executor, _http_get, path)
        # Only count requests that got a real HTTP status back.
        if status > 0:
            results.append((status, latency))


def urllib_quote(s: str) -> str:
    import urllib.parse
    return urllib.parse.quote_plus(s)


async def run_test(endpoint: str, duration_s: int, concurrency: int) -> dict[str, Any]:
    results: list[tuple[int, float]] = []
    loop = asyncio.get_running_loop()
    end_at = time.time() + duration_s
    start = time.time()

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        tasks = [
            asyncio.create_task(worker(endpoint, end_at, results, loop, executor))
            for _ in range(concurrency)
        ]
        await asyncio.gather(*tasks)

    elapsed = time.time() - start
    statuses = [s for s, _ in results]
    latencies = sorted([lat for _, lat in results])
    oks = sum(1 for s in statuses if 200 <= s < 300)
    errs = len(statuses) - oks

    def pct(p: float) -> float:
        if not latencies:
            return 0.0
        return round(latencies[min(len(latencies) - 1, int(len(latencies) * p))], 2)

    return {
        "endpoint": endpoint,
        "duration_s": round(elapsed, 2),
        "concurrency": concurrency,
        "total_requests": len(results),
        "ok": oks,
        "errors": errs,
        "rps": round(len(results) / elapsed, 1) if elapsed > 0 else 0,
        "mean_ms": round(statistics.mean(latencies), 2) if latencies else 0,
        "p50_ms": pct(0.50),
        "p95_ms": pct(0.95),
        "p99_ms": pct(0.99),
        "max_ms": round(max(latencies), 2) if latencies else 0,
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=15)
    parser.add_argument("--concurrency", type=int, default=50)
    args = parser.parse_args()

    endpoints = [
        ("/healthz", "/healthz"),
        ("/recall",   "/recall?q={q}&n=5"),
        ("/recall/v2", "/recall/v2?q={q}&n=5"),
    ]

    print(f"Running load test: duration={args.duration}s concurrency={args.concurrency}")
    print("=" * 68)

    results = []
    for name, template in endpoints:
        print(f"\n--- {name} ---")
        report = await run_test(template, args.duration, args.concurrency)
        results.append((name, report))
        print(f"  rps: {report['rps']}")
        print(f"  ok/err: {report['ok']}/{report['errors']}")
        print(f"  p50/p95/p99/max: {report['p50_ms']}/{report['p95_ms']}/{report['p99_ms']}/{report['max_ms']} ms")

    # Pass criteria
    print("\n" + "=" * 68)
    print("Pass criteria (Phase 5 plan):")
    recall_v2 = next((r for n, r in results if n == "/recall/v2"), None)
    if recall_v2:
        # Targets: 9-collection fan-out + embedding + RRF + rerank.
        # Single-user system — latency matters more than throughput.
        rps_pass = recall_v2["rps"] >= 10
        p95_pass = recall_v2["p95_ms"] < 500
        print(f"  /recall/v2 ≥10 rps: {'PASS' if rps_pass else 'FAIL'} (got {recall_v2['rps']})")
        print(f"  /recall/v2 p95 <500ms: {'PASS' if p95_pass else 'FAIL'} (got {recall_v2['p95_ms']}ms)")

    # Save raw report
    out = Path("/Users/chrischo/server/brain/tests/load-test-result.json")
    out.write_text(json.dumps({n: r for n, r in results}, indent=2))
    print(f"\nRaw report → {out}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(asyncio.run(main()))
