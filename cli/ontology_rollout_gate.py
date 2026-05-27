#!/usr/bin/env python3
"""Ontology rollout gate: drift audit + policy + retrieval regression + latency.

Use this before widening ontology expansion and as a scheduled guardrail after
production enablement. It is intentionally orchestration-only: each sub-gate
keeps its own focused logic and this script records a single pass/fail report.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "cli"
LOG_DIR = ROOT / "logs" / "ontology-gates"
SECRET_FILE = Path.home() / ".brain" / "credentials" / ".personal_webhook_secret"
DEFAULT_BRAIN_URL = "http://127.0.0.1:8791"


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _run_json(
    cmd: list[str], *, cwd: Path = ROOT, timeout: int = 180
) -> tuple[int, dict[str, Any], str, str]:
    started = time.time()
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    elapsed_ms = int((time.time() - started) * 1000)
    payload: dict[str, Any]
    try:
        payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except json.JSONDecodeError:
        payload = {"parse_error": "stdout was not JSON", "stdout_head": proc.stdout[:1000]}
    payload.setdefault("_elapsed_ms", elapsed_ms)
    return proc.returncode, payload, proc.stdout, proc.stderr


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)


def _bad_status_counts(section: dict[str, Any]) -> dict[str, int]:
    status = section.get("relations", section).get("status_counts", {}) or {}
    return {
        key: int(value)
        for key, value in status.items()
        if key in {"unknown", "deprecated", "blank"} and value
    }


def _audit_blockers(audit: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    for label in ("ontology_graph", "sqlite_entity_relations"):
        bad = _bad_status_counts(audit.get(label, {}))
        if bad:
            blockers.append(f"{label} has non-canonical relation statuses: {bad}")

    neo = audit.get("neo4j_relations", {})
    if neo.get("available"):
        bad = _bad_status_counts(neo.get("relation_types", {}))
        if bad:
            blockers.append(f"neo4j_relations has non-canonical relation statuses: {bad}")

    facts = audit.get("facts", {}).get("attributes", {}) or {}
    bad_facts = {
        attr: meta.get("attribute_status")
        for attr, meta in facts.items()
        if meta.get("attribute_status") in {"unknown", "deprecated", "blank"}
    }
    if bad_facts:
        blockers.append(f"facts have non-canonical attributes: {bad_facts}")
    return blockers


def _percentile(values: list[int], pct: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * pct))))
    return int(ordered[idx])


def _expansion_perf(expansion: dict[str, Any]) -> dict[str, Any]:
    ontology_ms = [
        int(row.get("on", {}).get("ontology_expansion_ms") or 0)
        for row in expansion.get("per_test", [])
        if row.get("on", {}).get("expanded")
    ]
    on_latencies = [int(row.get("on", {}).get("latency_ms") or 0) for row in expansion.get("per_test", [])]
    off_latencies = [int(row.get("off", {}).get("latency_ms") or 0) for row in expansion.get("per_test", [])]
    return {
        "ontology_expanded_cases": len(ontology_ms),
        "ontology_expansion_ms_p50": _percentile(ontology_ms, 0.50),
        "ontology_expansion_ms_p95": _percentile(ontology_ms, 0.95),
        "off_latency_ms_p95": _percentile(off_latencies, 0.95),
        "on_latency_ms_p95": _percentile(on_latencies, 0.95),
    }


def _expansion_blockers(
    expansion: dict[str, Any],
    *,
    max_p95_regression_pct: float,
    max_mean_regression_ms: float,
    max_ontology_p95_ms: int,
) -> list[str]:
    blockers: list[str] = []
    delta = expansion.get("delta", {}) or {}
    if float(delta.get("content_hit_pct") or 0) < 0:
        blockers.append(f"content hit regression: {delta.get('content_hit_pct')}pt")
    if float(delta.get("source_hit_pct") or 0) < 0:
        blockers.append(f"source hit regression: {delta.get('source_hit_pct')}pt")
    if float(delta.get("p95_latency_pct") or 0) > max_p95_regression_pct:
        blockers.append(f"p95 latency regression: {delta.get('p95_latency_pct')}%")
    if float(delta.get("mean_latency_ms") or 0) > max_mean_regression_ms:
        blockers.append(f"mean latency regression: {delta.get('mean_latency_ms')}ms")
    perf = _expansion_perf(expansion)
    if int(perf["ontology_expansion_ms_p95"]) > max_ontology_p95_ms:
        blockers.append(
            f"ontology expansion p95 too high: {perf['ontology_expansion_ms_p95']}ms > {max_ontology_p95_ms}ms"
        )
    return blockers


LIVE_CASES = [
    {"query": "nginx proxy route", "expected_applied": False},
    {"query": "searxng proxy route", "expected_applied": True},
    {"query": "neo4j dependency", "expected_applied": True},
    {"query": "brain system dependency", "expected_applied": False},
    {"query": "MCC owner", "expected_applied": True},
    {"query": "Jenna responsibilities", "expected_applied": False},
    {"query": "Chris Cho agents", "expected_applied": True},
]


def _live_smoke(
    brain_url: str,
    *,
    expansion_mode: str = "rewrite",
    timeout: int = 20,
    max_live_p95_ms: int = 1000,
    max_live_ontology_p95_ms: int = 150,
    live_retries: int = 1,
    live_retry_sleep_s: float = 1.0,
) -> dict[str, Any]:
    if not SECRET_FILE.exists():
        return {"available": False, "reason": f"missing token file: {SECRET_FILE}", "passed": False}
    token = SECRET_FILE.read_text().strip()
    results: list[dict[str, Any]] = []
    blockers: list[str] = []
    for case in LIVE_CASES:
        query = case["query"]
        url = f"{brain_url.rstrip('/')}/recall/v2?q={urllib.parse.quote(query)}&n=3"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})  # noqa: S310
        attempts: list[dict[str, Any]] = []
        selected: dict[str, Any] | None = None
        for attempt in range(max(0, live_retries) + 1):
            started = time.time()
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                    payload = json.load(resp)
            except Exception as exc:
                elapsed_ms = int((time.time() - started) * 1000)
                attempts.append({"attempt": attempt + 1, "latency_ms": elapsed_ms, "error": str(exc)})
                if attempt < max(0, live_retries):
                    time.sleep(max(0.0, live_retry_sleep_s))
                    continue
                blockers.append(f"{query}: live recall failed: {exc}")
                break
            elapsed_ms = int((time.time() - started) * 1000)
            timing = payload.get("timing", {}) or payload.get("source_timing", {}) or {}
            row = {
                "query": query,
                "latency_ms": int(payload.get("latency_ms") or timing.get("total_ms") or elapsed_ms),
                "ontology_applied": bool(timing.get("ontology_expansion_applied")),
                "ontology_terms": int(timing.get("ontology_expansion_terms") or 0),
                "ontology_ms": int(timing.get("ontology_expansion_ms") or 0),
                "top": [
                    result.get("title") or result.get("path") or result.get("collection")
                    for result in payload.get("results", [])[:3]
                ],
                "attempt": attempt + 1,
            }
            # Keep attempts as immutable snapshots. `selected` is appended to
            # final results below; storing the same dict object in attempts and
            # later attaching `selected["attempts"] = attempts` creates a
            # self-referential structure when a live-smoke retry happens, which
            # makes json.dumps fail with "Circular reference detected".
            attempts.append(dict(row))
            selected = row
            if row["latency_ms"] <= max_live_p95_ms or attempt >= max(0, live_retries):
                break
            time.sleep(max(0.0, live_retry_sleep_s))
        if not selected:
            continue

        applied = bool(selected["ontology_applied"])
        expected_applied = bool(case["expected_applied"])
        # In sidecar mode ontology terms are allowed to create a bounded
        # auxiliary RAG query without rewriting the primary query. The old
        # "must not apply" checks are rewrite-mode safety checks; policy and
        # retrieval gates still catch sidecar ranking/provenance regressions.
        enforce_applied = not (expansion_mode == "sidecar" and expected_applied is False)
        if enforce_applied and applied != expected_applied:
            blockers.append(f"{query}: expected applied={expected_applied}, got {applied}")
        if len(attempts) > 1:
            selected["attempts"] = attempts
        results.append(selected)
    latencies = [int(row["latency_ms"]) for row in results]
    ontology_latencies = [int(row["ontology_ms"]) for row in results if row.get("ontology_applied")]
    p95_latency_ms = _percentile(latencies, 0.95)
    p95_ontology_ms = _percentile(ontology_latencies, 0.95)
    if p95_latency_ms > max_live_p95_ms:
        blockers.append(f"live p95 latency too high: {p95_latency_ms}ms > {max_live_p95_ms}ms")
    if p95_ontology_ms > max_live_ontology_p95_ms:
        blockers.append(f"live ontology p95 too high: {p95_ontology_ms}ms > {max_live_ontology_p95_ms}ms")
    return {
        "available": True,
        "passed": not blockers,
        "blockers": blockers,
        "cases": len(results),
        "p50_latency_ms": _percentile(latencies, 0.50),
        "p95_latency_ms": p95_latency_ms,
        "p95_ontology_ms": p95_ontology_ms,
        "live_retries": max(0, live_retries),
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Ontology rollout regression/perf/latency gate")
    parser.add_argument("--artifacts-dir", type=Path, default=LOG_DIR)
    parser.add_argument("--limit", type=int, default=138)
    parser.add_argument("--n", type=int, default=5)
    parser.add_argument("--source", choices=["neo4j", "file"], default="neo4j")
    parser.add_argument("--relations", default="has_agent,owned_by,owns")
    parser.add_argument("--mode", choices=["rewrite", "sidecar"], default="rewrite")
    parser.add_argument("--sidecar-limit", type=int, default=5)
    parser.add_argument("--conditional", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--live", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--brain-url", default=DEFAULT_BRAIN_URL)
    parser.add_argument("--max-p95-regression-pct", type=float, default=10.0)
    parser.add_argument("--max-mean-regression-ms", type=float, default=25.0)
    parser.add_argument("--max-ontology-p95-ms", type=int, default=75)
    parser.add_argument("--max-live-p95-ms", type=int, default=1000)
    parser.add_argument("--max-live-ontology-p95-ms", type=int, default=150)
    parser.add_argument("--live-retries", type=int, default=1)
    parser.add_argument("--live-retry-sleep", type=float, default=1.0)
    parser.add_argument("--stale-current-truth", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    stamp = _timestamp()
    args.artifacts_dir.mkdir(parents=True, exist_ok=True)

    failures: list[str] = []
    artifacts: dict[str, str] = {}

    audit_cmd = [sys.executable, str(CLI / "audit_ontology.py"), "--json"]
    audit_code, audit, audit_stdout, audit_stderr = _run_json(audit_cmd, timeout=120)
    audit_path = args.artifacts_dir / f"ontology-audit-{stamp}.json"
    _write_text(audit_path, json.dumps(audit, indent=2, ensure_ascii=False))
    artifacts["audit"] = str(audit_path)
    if audit_code != 0:
        failures.append(f"audit command failed with exit {audit_code}: {audit_stderr[:500]}")
    failures.extend(_audit_blockers(audit))

    stale_current_truth: dict[str, Any] = {"skipped": True}
    if args.stale_current_truth:
        stale_cmd = [
            sys.executable,
            str(CLI / "audit_stale_current_truth.py"),
            "--json",
            "--scan-vector",
            "--scan-atoms",
            "--fail-on-blockers",
        ]
        stale_code, stale_current_truth, stale_stdout, stale_stderr = _run_json(stale_cmd, timeout=120)
        stale_path = args.artifacts_dir / f"stale-current-truth-{stamp}.json"
        _write_text(stale_path, json.dumps(stale_current_truth, indent=2, ensure_ascii=False))
        artifacts["stale_current_truth"] = str(stale_path)
        if stale_code != 0 or not stale_current_truth.get("passed"):
            failures.append(
                "stale current-truth gate failed: "
                f"exit={stale_code} blockers={stale_current_truth.get('blocker_count')} stderr={stale_stderr[:500]}"
            )

    policy_cmd = [
        sys.executable,
        str(CLI / "eval_ontology_policy.py"),
        "--source",
        args.source,
        "--relations",
        args.relations,
        "--json",
    ]
    policy_code, policy, policy_stdout, policy_stderr = _run_json(policy_cmd, timeout=120)
    policy_path = args.artifacts_dir / f"ontology-policy-{stamp}.json"
    _write_text(policy_path, json.dumps(policy, indent=2, ensure_ascii=False))
    artifacts["policy"] = str(policy_path)
    if policy_code != 0 or not policy.get("passed"):
        failures.append(f"policy gate failed: exit={policy_code} stderr={policy_stderr[:500]}")

    expansion_cmd = [
        sys.executable,
        str(CLI / "eval_ontology_expansion.py"),
        "--limit",
        str(args.limit),
        "--n",
        str(args.n),
        "--source",
        args.source,
        "--relations",
        args.relations,
        "--mode",
        args.mode,
        "--sidecar-limit",
        str(args.sidecar_limit),
        "--fail-on-regression",
        "--max-p95-regression-pct",
        str(args.max_p95_regression_pct),
        "--json",
    ]
    if args.conditional:
        expansion_cmd.append("--conditional")
    expansion_code, expansion, expansion_stdout, expansion_stderr = _run_json(expansion_cmd, timeout=360)
    expansion_path = args.artifacts_dir / f"ontology-expansion-ab-{stamp}.json"
    _write_text(expansion_path, json.dumps(expansion, indent=2, ensure_ascii=False))
    artifacts["expansion"] = str(expansion_path)
    if expansion_code != 0:
        failures.append(
            f"retrieval expansion gate failed with exit {expansion_code}: {expansion_stderr[:500]}"
        )
    failures.extend(
        _expansion_blockers(
            expansion,
            max_p95_regression_pct=args.max_p95_regression_pct,
            max_mean_regression_ms=args.max_mean_regression_ms,
            max_ontology_p95_ms=args.max_ontology_p95_ms,
        )
    )

    live: dict[str, Any] = {"skipped": True}
    if args.live:
        live = _live_smoke(
            args.brain_url,
            expansion_mode=args.mode,
            max_live_p95_ms=args.max_live_p95_ms,
            max_live_ontology_p95_ms=args.max_live_ontology_p95_ms,
            live_retries=args.live_retries,
            live_retry_sleep_s=args.live_retry_sleep,
        )
        live_path = args.artifacts_dir / f"ontology-live-smoke-{stamp}.json"
        _write_text(live_path, json.dumps(live, indent=2, ensure_ascii=False))
        artifacts["live_smoke"] = str(live_path)
        if not live.get("passed"):
            failures.extend(live.get("blockers") or ["live smoke failed"])

    report = {
        "timestamp": stamp,
        "passed": not failures,
        "failures": failures,
        "config": {
            "source": args.source,
            "relations": [rel.strip() for rel in args.relations.split(",") if rel.strip()],
            "mode": args.mode,
            "sidecar_limit": args.sidecar_limit,
            "conditional": args.conditional,
            "limit": args.limit,
            "n": args.n,
            "max_p95_regression_pct": args.max_p95_regression_pct,
            "max_mean_regression_ms": args.max_mean_regression_ms,
            "max_ontology_p95_ms": args.max_ontology_p95_ms,
            "max_live_p95_ms": args.max_live_p95_ms,
            "max_live_ontology_p95_ms": args.max_live_ontology_p95_ms,
            "live_retries": args.live_retries,
            "live_retry_sleep": args.live_retry_sleep,
            "stale_current_truth": args.stale_current_truth,
        },
        "summary": {
            "audit_blockers": _audit_blockers(audit),
            "stale_current_truth": {
                k: stale_current_truth.get(k)
                for k in (
                    "passed",
                    "files_scanned",
                    "skipped_archived",
                    "historical_mentions_allowed",
                    "blocker_count",
                )
            },
            "stale_vector": {
                k: (stale_current_truth.get("vector") or {}).get(k)
                for k in ("passed", "blocker_count", "collections_scanned", "collections_skipped")
            },
            "stale_atoms": {
                k: (stale_current_truth.get("atoms") or {}).get(k)
                for k in (
                    "passed",
                    "blocker_count",
                    "atoms_scanned",
                    "superseded_valid_until_missing",
                    "marked_atoms",
                )
            },
            "policy_passed": bool(policy.get("passed")),
            "retrieval_delta": expansion.get("delta", {}),
            "retrieval_off": expansion.get("off", {}),
            "retrieval_on": expansion.get("on", {}),
            "expansion_perf": _expansion_perf(expansion),
            "live": {
                k: live.get(k)
                for k in (
                    "available",
                    "passed",
                    "cases",
                    "p50_latency_ms",
                    "p95_latency_ms",
                    "p95_ontology_ms",
                )
            },
        },
        "artifacts": artifacts,
    }
    latest_path = args.artifacts_dir / "ontology-rollout-latest.json"
    report_path = args.artifacts_dir / f"ontology-rollout-{stamp}.json"
    _write_text(report_path, json.dumps(report, indent=2, ensure_ascii=False))
    _write_text(latest_path, json.dumps(report, indent=2, ensure_ascii=False))
    artifacts["report"] = str(report_path)
    artifacts["latest"] = str(latest_path)

    print(
        json.dumps(report, indent=2, ensure_ascii=False)
        if args.json
        else json.dumps(report["summary"], indent=2, ensure_ascii=False)
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
