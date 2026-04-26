#!/usr/bin/env python3
"""Sweep ontology expansion relation candidates with regression/latency metrics.

This is the safe "try before production" layer for widening ontology query
expansion. It never changes launchd/config; it only runs A/B gates and writes a
ranked recommendation report.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "cli"
DEFAULT_ARTIFACTS_DIR = ROOT / "logs" / "ontology-gates"
BASE_RELATIONS = ("has_agent", "owned_by", "owns")


DEFAULT_CANDIDATES: tuple[tuple[str, tuple[str, ...], str, int], ...] = (
    ("current_conditional", BASE_RELATIONS, "rewrite", 5),
    ("base_plus_has_event", (*BASE_RELATIONS, "has_event"), "rewrite", 5),
    ("base_plus_prefers", (*BASE_RELATIONS, "prefers"), "rewrite", 5),
    ("base_plus_proxies_always_on", (*BASE_RELATIONS, "proxies"), "rewrite", 5),
    ("base_plus_depends_on_always_on", (*BASE_RELATIONS, "depends_on"), "rewrite", 5),
    ("base_plus_manages_always_on", (*BASE_RELATIONS, "manages"), "rewrite", 5),
    (
        "full_typed_candidate",
        (*BASE_RELATIONS, "proxies", "depends_on", "manages", "has_event", "prefers"),
        "rewrite",
        5,
    ),
    (
        "full_typed_sidecar_candidate",
        (*BASE_RELATIONS, "proxies", "depends_on", "manages", "has_event", "prefers"),
        "sidecar",
        5,
    ),
    (
        "full_typed_sidecar_tiny_candidate",
        (*BASE_RELATIONS, "proxies", "depends_on", "manages", "has_event", "prefers"),
        "sidecar",
        2,
    ),
)


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _percentile(values: list[int], pct: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * pct))))
    return int(ordered[idx])


def _expansion_perf(report: dict[str, Any]) -> dict[str, Any]:
    ontology_ms = [
        int(row.get("on", {}).get("ontology_expansion_ms") or 0)
        for row in report.get("per_test", [])
        if row.get("on", {}).get("expanded")
    ]
    return {
        "ontology_expanded_cases": len(ontology_ms),
        "ontology_expansion_ms_p50": _percentile(ontology_ms, 0.50),
        "ontology_expansion_ms_p95": _percentile(ontology_ms, 0.95),
    }


def _run_candidate(
    args: argparse.Namespace, stamp: str, name: str, relations: tuple[str, ...], mode: str, sidecar_limit: int
) -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(CLI / "eval_ontology_expansion.py"),
        "--limit",
        str(args.limit),
        "--n",
        str(args.n),
        "--source",
        args.source,
        "--relations",
        ",".join(relations),
        "--mode",
        mode,
        "--sidecar-limit",
        str(sidecar_limit),
        "--json",
    ]
    if args.conditional:
        cmd.append("--conditional")

    started = time.time()
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=args.timeout)
    elapsed_ms = int((time.time() - started) * 1000)
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        report = {"parse_error": proc.stdout[:1000]}
    raw_path = args.artifacts_dir / f"ontology-candidate-{name}-{stamp}.json"
    raw_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    delta = report.get("delta", {}) or {}
    on = report.get("on", {}) or {}
    perf = _expansion_perf(report)
    failures: list[str] = []
    if proc.returncode != 0:
        failures.append(f"eval command exit {proc.returncode}")
    if float(delta.get("content_hit_pct") or 0) < 0:
        failures.append(f"content_hit {delta.get('content_hit_pct')}pt")
    if float(delta.get("source_hit_pct") or 0) < 0:
        failures.append(f"source_hit {delta.get('source_hit_pct')}pt")
    if float(delta.get("p95_latency_pct") or 0) > args.max_p95_regression_pct:
        failures.append(f"p95_latency {delta.get('p95_latency_pct')}%")
    if float(delta.get("mean_latency_ms") or 0) > args.max_mean_regression_ms:
        failures.append(f"mean_latency {delta.get('mean_latency_ms')}ms")
    if int(perf["ontology_expansion_ms_p95"]) > args.max_ontology_p95_ms:
        failures.append(f"ontology_p95 {perf['ontology_expansion_ms_p95']}ms")

    expanded_pct = float(on.get("expanded_pct") or 0)
    if failures:
        decision = "reject_regression"
    elif expanded_pct < args.min_meaningful_expanded_pct:
        decision = "pass_no_enable_low_incremental_value"
    else:
        decision = "pass_candidate_for_manual_or_config_enable"

    return {
        "name": name,
        "relations": list(relations),
        "conditional": bool(args.conditional),
        "mode": mode,
        "sidecar_limit": sidecar_limit,
        "exit_code": proc.returncode,
        "elapsed_ms": elapsed_ms,
        "decision": decision,
        "failures": failures,
        "off": report.get("off", {}),
        "on": on,
        "delta": delta,
        "perf": perf,
        "stderr_head": proc.stderr[:1000],
        "raw_artifact": str(raw_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Ontology relation candidate sweep")
    parser.add_argument("--artifacts-dir", type=Path, default=DEFAULT_ARTIFACTS_DIR)
    parser.add_argument("--limit", type=int, default=138)
    parser.add_argument("--n", type=int, default=5)
    parser.add_argument("--source", choices=["neo4j", "file"], default="neo4j")
    parser.add_argument("--conditional", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--timeout", type=int, default=360)
    parser.add_argument("--max-p95-regression-pct", type=float, default=10.0)
    parser.add_argument("--max-mean-regression-ms", type=float, default=25.0)
    parser.add_argument("--max-ontology-p95-ms", type=int, default=75)
    parser.add_argument("--min-meaningful-expanded-pct", type=float, default=2.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    args.artifacts_dir.mkdir(parents=True, exist_ok=True)
    stamp = _timestamp()
    results = [
        _run_candidate(args, stamp, name, relations, mode, sidecar_limit)
        for name, relations, mode, sidecar_limit in DEFAULT_CANDIDATES
    ]

    viable = [row for row in results if row["decision"] == "pass_candidate_for_manual_or_config_enable"]
    rejected = [row for row in results if row["decision"] == "reject_regression"]
    no_value = [row for row in results if row["decision"] == "pass_no_enable_low_incremental_value"]
    recommendation = (
        "enable_best_candidate_after_live_gate" if viable else "keep_current_production_allowlist"
    )
    report = {
        "timestamp": stamp,
        "source": args.source,
        "conditional": bool(args.conditional),
        "cases": args.limit,
        "thresholds": {
            "max_p95_regression_pct": args.max_p95_regression_pct,
            "max_mean_regression_ms": args.max_mean_regression_ms,
            "max_ontology_p95_ms": args.max_ontology_p95_ms,
            "min_meaningful_expanded_pct": args.min_meaningful_expanded_pct,
        },
        "recommendation": recommendation,
        "best_candidate": viable[0] if viable else None,
        "counts": {
            "viable": len(viable),
            "rejected": len(rejected),
            "pass_no_value": len(no_value),
        },
        "results": results,
    }

    path = args.artifacts_dir / f"ontology-candidate-sweep-{stamp}.json"
    latest = args.artifacts_dir / "ontology-candidate-sweep-latest.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    latest.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(
            json.dumps(
                {
                    "recommendation": recommendation,
                    "counts": report["counts"],
                    "results": [
                        {
                            "name": row["name"],
                            "decision": row["decision"],
                            "expanded_pct": row.get("on", {}).get("expanded_pct"),
                            "delta": row["delta"],
                            "perf": row["perf"],
                            "failures": row["failures"],
                        }
                        for row in results
                    ],
                    "artifact": str(path),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
