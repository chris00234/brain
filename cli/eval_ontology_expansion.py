#!/usr/bin/env python3
"""A/B gate for ontology query expansion in search_unified.

Runs search_unified.search_all in-process with ontology expansion off vs on.
This is intentionally a lightweight local gate before enabling
BRAIN_ONTOLOGY_EXPANSION_ENABLED in launchd.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BRAIN_CORE = ROOT / "brain_core"
if str(BRAIN_CORE) not in sys.path:
    sys.path.insert(0, str(BRAIN_CORE))

import search_unified  # noqa: E402

DEFAULT_EVAL_SET = ROOT / "cli" / "eval_set_stable.json"


def _contains_expected(results: list[dict[str, Any]], expected: str) -> bool:
    if not expected:
        return False
    needle = expected.lower()
    for result in results:
        haystack = " ".join(
            [
                str(result.get("title") or ""),
                str(result.get("content") or ""),
                str(result.get("path") or ""),
                json.dumps(result.get("metadata") or {}, ensure_ascii=False),
            ]
        ).lower()
        if needle in haystack:
            return True
    return False


def _source_hit(results: list[dict[str, Any]], expected: str) -> bool:
    if not expected:
        return False
    needle = expected.lower()
    for result in results:
        haystack = " ".join(
            [
                str(result.get("collection") or ""),
                str(result.get("source_type") or ""),
                str(result.get("path") or ""),
                str((result.get("provenance") or {}).get("tier") or ""),
            ]
        ).lower()
        if needle in haystack:
            return True
    return False


def _percentile(values: list[int], pct: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * pct))))
    return int(ordered[idx])


def _run_one(query: str, *, enabled: bool, limit: int) -> dict[str, Any]:
    search_unified.BRAIN_ONTOLOGY_EXPANSION_ENABLED = enabled
    start = time.time()
    payload = search_unified.search_all(query, limit=limit, original_query=query)
    latency_ms = int((time.time() - start) * 1000)
    return {
        "latency_ms": latency_ms,
        "results": payload.get("results", []),
        "source_timing": payload.get("source_timing", {}),
        "expanded": bool(payload.get("source_timing", {}).get("ontology_expansion_applied")),
        "expansion_terms": int(payload.get("source_timing", {}).get("ontology_expansion_terms") or 0),
    }


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    latencies = [int(row["latency_ms"]) for row in rows]
    total = len(rows)
    return {
        "total": total,
        "content_hit_pct": round(100 * sum(1 for row in rows if row["content_hit"]) / total, 1)
        if total
        else 0.0,
        "source_hit_pct": round(100 * sum(1 for row in rows if row["source_hit"]) / total, 1)
        if total
        else 0.0,
        "expanded_pct": round(100 * sum(1 for row in rows if row["expanded"]) / total, 1) if total else 0.0,
        "mean_latency_ms": round(statistics.mean(latencies), 1) if latencies else 0,
        "p50_latency_ms": _percentile(latencies, 0.50),
        "p95_latency_ms": _percentile(latencies, 0.95),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="A/B gate for ontology recall expansion")
    parser.add_argument("--eval-set", type=Path, default=DEFAULT_EVAL_SET)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--n", type=int, default=5)
    parser.add_argument(
        "--relations",
        default="",
        help="Comma-separated ontology expansion relation allowlist to test; defaults to module config.",
    )
    parser.add_argument(
        "--source",
        choices=["neo4j", "file"],
        default="",
        help="Ontology expansion source to test; defaults to module config.",
    )
    parser.add_argument(
        "--conditional", action="store_true", help="Enable intent-conditioned relation expansion"
    )
    parser.add_argument(
        "--mode",
        choices=["rewrite", "sidecar"],
        default="",
        help="Ontology expansion mode to test; rewrite mutates the query, sidecar adds a bounded RAG-only query.",
    )
    parser.add_argument(
        "--sidecar-limit", type=int, default=0, help="Override sidecar candidate limit for tests"
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--fail-on-regression", action="store_true")
    parser.add_argument("--max-p95-regression-pct", type=float, default=10.0)
    args = parser.parse_args()

    cases = json.loads(args.eval_set.read_text())
    if args.limit > 0:
        cases = cases[: args.limit]
    if args.relations:
        search_unified.BRAIN_ONTOLOGY_EXPANSION_RELATIONS = tuple(
            rel.strip() for rel in args.relations.split(",") if rel.strip()
        )
        search_unified._ontology_cache = None
        search_unified._ontology_cache_ts = 0.0
    if args.source:
        search_unified.BRAIN_ONTOLOGY_EXPANSION_SOURCE = args.source
        search_unified._ontology_cache = None
        search_unified._ontology_cache_ts = 0.0
    if args.conditional:
        search_unified.BRAIN_ONTOLOGY_CONDITIONAL_EXPANSION_ENABLED = True
        search_unified._ontology_cache = None
        search_unified._ontology_cache_ts = 0.0
    if args.mode:
        search_unified.BRAIN_ONTOLOGY_EXPANSION_MODE = args.mode
    if args.sidecar_limit > 0:
        search_unified.BRAIN_ONTOLOGY_SIDECAR_LIMIT = args.sidecar_limit

    rows: list[dict[str, Any]] = []
    for idx, case in enumerate(cases):
        query = str(case.get("query") or "")
        if not query:
            continue
        expected_content = str(case.get("expected_content") or "")
        expected_source = str(case.get("expected_source") or "")
        # Warm both branches once and alternate measured order to avoid
        # mistaking cache warmup for ontology benefit/regression.
        _run_one(query, enabled=False, limit=args.n)
        _run_one(query, enabled=True, limit=args.n)
        if idx % 2 == 0:
            off = _run_one(query, enabled=False, limit=args.n)
            on = _run_one(query, enabled=True, limit=args.n)
        else:
            on = _run_one(query, enabled=True, limit=args.n)
            off = _run_one(query, enabled=False, limit=args.n)
        rows.append(
            {
                "query": query,
                "off": {
                    "latency_ms": off["latency_ms"],
                    "content_hit": _contains_expected(off["results"], expected_content),
                    "source_hit": _source_hit(off["results"], expected_source),
                    "expanded": off["expanded"],
                    "expansion_terms": off["expansion_terms"],
                },
                "on": {
                    "latency_ms": on["latency_ms"],
                    "content_hit": _contains_expected(on["results"], expected_content),
                    "source_hit": _source_hit(on["results"], expected_source),
                    "expanded": on["expanded"],
                    "expansion_terms": on["expansion_terms"],
                    "ontology_expansion_ms": on["source_timing"].get("ontology_expansion_ms", 0),
                },
            }
        )

    off_rows = [dict(row["off"], query=row["query"]) for row in rows]
    on_rows = [dict(row["on"], query=row["query"]) for row in rows]
    off = _aggregate(off_rows)
    on = _aggregate(on_rows)
    p95_delta_pct = (
        round(100 * (on["p95_latency_ms"] - off["p95_latency_ms"]) / off["p95_latency_ms"], 1)
        if off["p95_latency_ms"]
        else 0.0
    )
    report = {
        "eval_set": str(args.eval_set),
        "cases": len(rows),
        "source": search_unified.BRAIN_ONTOLOGY_EXPANSION_SOURCE,
        "relations": list(search_unified.BRAIN_ONTOLOGY_EXPANSION_RELATIONS),
        "conditional": bool(search_unified.BRAIN_ONTOLOGY_CONDITIONAL_EXPANSION_ENABLED),
        "mode": search_unified.BRAIN_ONTOLOGY_EXPANSION_MODE,
        "sidecar_limit": search_unified.BRAIN_ONTOLOGY_SIDECAR_LIMIT,
        "off": off,
        "on": on,
        "delta": {
            "content_hit_pct": round(on["content_hit_pct"] - off["content_hit_pct"], 1),
            "source_hit_pct": round(on["source_hit_pct"] - off["source_hit_pct"], 1),
            "p95_latency_pct": p95_delta_pct,
            "mean_latency_ms": round(on["mean_latency_ms"] - off["mean_latency_ms"], 1),
        },
        "per_test": rows,
    }

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(json.dumps({k: v for k, v in report.items() if k != "per_test"}, indent=2, ensure_ascii=False))

    if args.fail_on_regression:
        if report["delta"]["content_hit_pct"] < 0:
            return 1
        if report["delta"]["source_hit_pct"] < 0:
            return 1
        if report["delta"]["p95_latency_pct"] > args.max_p95_regression_pct:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
