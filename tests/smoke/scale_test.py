#!/opt/homebrew/bin/python3
"""Scale test for brain system — generates synthetic data, measures SLOs."""

from __future__ import annotations

import argparse
import json
import random
import statistics
import time
import urllib.parse
import urllib.request
from pathlib import Path

BRAIN_URL = "http://127.0.0.1:8791"
SECRET_FILE = Path("/Users/chrischo/.openclaw/credentials/.personal_webhook_secret")
BASELINE_FILE = Path("/Users/chrischo/server/brain/tests/slo_baseline.json")

# Synthetic content templates
TOPICS = [
    "docker",
    "python",
    "neo4j",
    "chromadb",
    "openclaw",
    "brain",
    "agent",
    "memory",
    "search",
    "canonical",
]
ACTIONS = ["configures", "implements", "tests", "debugs", "deploys", "monitors", "refactors"]
OBJECTS = ["service", "pipeline", "endpoint", "database", "cache", "index", "schema"]


def gen_memory(i: int) -> str:
    topic = random.choice(TOPICS)
    action = random.choice(ACTIONS)
    obj = random.choice(OBJECTS)
    return f"Chris {action} the {topic} {obj} for integration test #{i}"


def gen_queries(n: int) -> list[str]:
    return [f"how does {random.choice(TOPICS)} work" for _ in range(n)]


def post_json(url: str, payload: dict, token: str, timeout: int = 30) -> dict:
    req = urllib.request.Request(
        url,
        method="POST",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def get_json(url: str, token: str, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def delete_memory(mem_id: str, token: str, timeout: int = 5) -> None:
    req = urllib.request.Request(
        f"{BRAIN_URL}/memory/{urllib.parse.quote(mem_id, safe='')}",
        method="DELETE",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        r.read()


def stats(latencies: list[float]) -> dict:
    if not latencies:
        return {"count": 0}
    s = sorted(latencies)
    n = len(s)
    return {
        "count": n,
        "p50": round(s[n // 2], 1),
        "p95": round(s[min(n - 1, int(n * 0.95))], 1),
        "p99": round(s[min(n - 1, int(n * 0.99))], 1) if n >= 100 else None,
        "mean": round(statistics.mean(s), 1),
        "max": round(s[-1], 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Synthetic scale test for brain system")
    parser.add_argument("--docs", type=int, default=1000, help="Number of synthetic memories to insert")
    parser.add_argument("--queries", type=int, default=100, help="Number of queries to run")
    parser.add_argument("--quick", action="store_true", help="Skip ingestion (measure-only mode)")
    parser.add_argument("--no-cleanup", action="store_true", help="Skip cleanup of test memories")
    args = parser.parse_args()

    if not SECRET_FILE.exists():
        print(f"ERROR: secret file not found: {SECRET_FILE}")
        return 1
    token = SECRET_FILE.read_text().strip()

    inserted_ids: list[str] = []
    ingest_throughput: float | None = None
    ingest_duration: float = 0.0

    if not args.quick:
        print(f"Inserting {args.docs} synthetic memories...")
        start = time.time()
        for i in range(args.docs):
            try:
                res = post_json(
                    f"{BRAIN_URL}/memory",
                    {
                        "content": gen_memory(i),
                        "category": "fact",
                        "agent": "scale_test",
                        "source": "scale_test",
                    },
                    token,
                )
                if res.get("id"):
                    inserted_ids.append(res["id"])
            except Exception as e:
                print(f"  insert {i} failed: {e}")
            if i > 0 and i % 100 == 0:
                elapsed = time.time() - start
                rate = i / elapsed if elapsed > 0 else 0
                print(f"  inserted {i}/{args.docs} ({rate:.1f} docs/sec)")
        ingest_duration = time.time() - start
        ingest_throughput = len(inserted_ids) / ingest_duration if ingest_duration > 0 else 0
        print(f"Ingested {len(inserted_ids)} in {ingest_duration:.1f}s ({ingest_throughput:.1f} docs/sec)")

    # Query latency
    print(f"\nRunning {args.queries} queries against /recall...")
    queries = gen_queries(args.queries)

    recall_latencies: list[float] = []
    recall_errors = 0
    for q in queries:
        url = f"{BRAIN_URL}/recall?q={urllib.parse.quote_plus(q)}&n=5"
        start = time.time()
        try:
            get_json(url, token)
            recall_latencies.append((time.time() - start) * 1000)
        except Exception:
            recall_errors += 1

    # v2 is slower, do fewer (min(20, queries))
    v2_count = min(20, args.queries)
    print(f"Running {v2_count} queries against /recall/v2...")
    recall_v2_latencies: list[float] = []
    recall_v2_errors = 0
    for q in queries[:v2_count]:
        url = f"{BRAIN_URL}/recall/v2?q={urllib.parse.quote_plus(q)}&n=5"
        start = time.time()
        try:
            get_json(url, token)
            recall_v2_latencies.append((time.time() - start) * 1000)
        except Exception:
            recall_v2_errors += 1

    baseline = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "docs_inserted": len(inserted_ids),
        "ingest_duration_sec": round(ingest_duration, 1) if ingest_duration else None,
        "ingest_throughput_docs_per_sec": round(ingest_throughput, 1) if ingest_throughput else None,
        "recall": {**stats(recall_latencies), "errors": recall_errors},
        "recall_v2": {**stats(recall_v2_latencies), "errors": recall_v2_errors},
    }

    BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_FILE.write_text(json.dumps(baseline, indent=2))
    print(f"\nBaseline written: {BASELINE_FILE}")
    print(json.dumps(baseline, indent=2))

    # Cleanup
    if inserted_ids and not args.quick and not args.no_cleanup:
        # Cap cleanup for safety — if test exploded, don't make it worse.
        cleanup_limit = min(len(inserted_ids), args.docs)
        print(f"\nCleaning up {cleanup_limit} test memories...")
        cleaned = 0
        for mem_id in inserted_ids[:cleanup_limit]:
            try:
                delete_memory(mem_id, token)
                cleaned += 1
            except Exception:
                pass
        print(f"Cleaned up {cleaned}/{cleanup_limit}")

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
