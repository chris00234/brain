#!/usr/bin/env python3
"""CRAG correction-quality regression gate.

``crag_regression.py`` proves the confidence evaluator does not silently accept
bad context. This gate proves the next hop can recover useful context when a
corrective path is chosen.

Default mode is deterministic: it uses explicit correction queries from a small
holdout file, so the scheduled readiness signal is cheap and repeatable. LLM
mode samples the live ``crag.expand_query`` rewrite path and writes a separate
report; it is intended to expose real rewrite quality without overwriting the
stable deterministic readiness artifact.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BRAIN_ROOT = Path(__file__).resolve().parents[1]
log = logging.getLogger("brain.crag_correction_regression")
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))

REPORT_FILE = BRAIN_ROOT / "logs" / "crag_correction_regression.json"
LLM_REPORT_FILE = BRAIN_ROOT / "logs" / "crag_llm_correction_regression.json"
DEFAULT_EVAL_SET = BRAIN_ROOT / "cli" / "eval_set_crag_corrections.json"
DEFAULT_MIN_RECOVERY_RATE = 80.0
DEFAULT_MIN_RECOVERY_CASES = 3
DEFAULT_MAX_MEAN_LATENCY_MS = 2500
DEFAULT_LLM_MAX_MEAN_LATENCY_MS = 15000
DEFAULT_TOP_K = 5


def _env_float(name: str, default: float, *, low: float = 0.0, high: float = 100.0) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return min(max(value, low), high)


def _env_int(name: str, default: int, *, low: int = 0) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(value, low)


def _load_cases(path: Path, limit: int) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("eval_set_root_not_list")
    cases = [r for r in data if isinstance(r, dict) and r.get("query")]
    if limit > 0:
        return cases[:limit]
    return cases


def _result_text(result: Any) -> str:
    if isinstance(result, dict):
        parts = [result.get(k) for k in ("title", "content", "path", "source", "collection", "source_type")]
        meta = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        parts.extend(meta.get(k) for k in ("path", "source", "source_type", "title", "category", "domain"))
        return " ".join(str(p) for p in parts if p).lower()
    return str(result).lower()


def _expected_hits(case: dict[str, Any], results: list[Any]) -> tuple[bool, bool, bool]:
    haystack = "\n".join(_result_text(r) for r in results)
    expected_content = str(case.get("expected_content") or "").lower().strip()
    expected_source = str(case.get("expected_source") or "").lower().strip()
    alternates_raw = case.get("expected_alternates") or []
    alternates = [str(v).lower().strip() for v in alternates_raw if str(v).strip()]
    content_hit = bool(expected_content and expected_content in haystack)
    source_hit = bool(expected_source and expected_source in haystack)
    alternate_hit = any(alt in haystack for alt in alternates)
    return content_hit, source_hit, alternate_hit


def _normalize_collection(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    if not normalized or normalized.lower() in {"all", "*"}:
        return None
    return normalized


def _search(query: str, collection: str | None, top_k: int) -> list[Any]:
    import search_unified

    kwargs: dict[str, Any] = {"limit": top_k}
    if collection:
        kwargs["collections"] = [collection]
    results = search_unified.search_all(query, **kwargs)
    if isinstance(results, dict):
        for key in ("results", "items"):
            if isinstance(results.get(key), list):
                return results[key]
        return []
    if isinstance(results, list):
        return results
    return []


def _attempt(
    query: str, case: dict[str, Any], collection: str | None, top_k: int
) -> tuple[dict[str, Any], list[Any]]:
    from crag import score_confidence, should_iterate

    t0 = time.time()
    results = _search(query, collection, top_k)
    content_hit, source_hit, alternate_hit = _expected_hits(case, results)
    confidence = score_confidence([r for r in results if isinstance(r, dict)], query=query)
    return (
        {
            "query": query,
            "ok": content_hit or source_hit or alternate_hit,
            "content_hit": content_hit,
            "source_hit": source_hit,
            "alternate_hit": alternate_hit,
            "result_count": len(results),
            "confidence": confidence.score,
            "should_iterate": should_iterate(confidence),
            "latency_ms": int((time.time() - t0) * 1000),
        },
        results,
    )


def _llm_rewrite(query: str, weak_results: list[dict[str, Any]], timeout_s: float) -> str | None:
    from crag import expand_query

    return expand_query(query, weak_results, timeout_s=timeout_s)


def _live_rewrite_candidates(
    query: str, weak_results: list[dict[str, Any]], timeout_s: float
) -> list[dict[str, Any]]:
    try:
        from crag import expand_query_candidates

        candidates = expand_query_candidates(query, weak_results, timeout_s=timeout_s)
        if candidates:
            return [dict(c) for c in candidates if c.get("query")]
    except Exception as exc:
        log.debug("live CRAG candidate expansion failed: %s", exc)
    rewritten = _llm_rewrite(query, weak_results, timeout_s)
    return [{"source": "llm", "query": rewritten}] if rewritten else []


def _correction_queries_for_case(
    *,
    case: dict[str, Any],
    query: str,
    initial_results: list[Any],
    rewrite_source: str,
    llm_timeout_s: float,
) -> list[dict[str, Any]]:
    if rewrite_source == "deterministic":
        return [{"source": "deterministic", "query": str(q)} for q in case.get("correction_queries") or []]
    weak_results = [r for r in initial_results if isinstance(r, dict)]
    t0 = time.time()
    candidates = _live_rewrite_candidates(query, weak_results, llm_timeout_s)
    rewrite_latency_ms = int((time.time() - t0) * 1000)
    return [
        {
            **candidate,
            "source": candidate.get("source") or "llm",
            "rewrite_latency_ms": rewrite_latency_ms,
            "rewrite_timeout_s": llm_timeout_s,
        }
        for candidate in candidates
        if str(candidate.get("query") or "").strip()
    ]


def run(
    eval_set: Path = DEFAULT_EVAL_SET,
    *,
    limit: int = 0,
    top_k: int = DEFAULT_TOP_K,
    rewrite_source: str = "deterministic",
    llm_timeout_s: float = 8.0,
) -> dict[str, Any]:
    if rewrite_source not in {"deterministic", "llm"}:
        raise ValueError("rewrite_source must be deterministic or llm")

    started = time.time()
    min_recovery_rate = _env_float("BRAIN_CRAG_CORRECTION_MIN_RECOVERY_RATE", DEFAULT_MIN_RECOVERY_RATE)
    min_recovery_cases = _env_int("BRAIN_CRAG_CORRECTION_MIN_CASES", DEFAULT_MIN_RECOVERY_CASES)
    max_latency_default = (
        DEFAULT_LLM_MAX_MEAN_LATENCY_MS if rewrite_source == "llm" else DEFAULT_MAX_MEAN_LATENCY_MS
    )
    max_mean_latency_ms = _env_int("BRAIN_CRAG_CORRECTION_MAX_MEAN_LATENCY_MS", max_latency_default)

    rows: list[dict[str, Any]] = []
    for case in _load_cases(eval_set, limit):
        query = str(case.get("query") or "")
        collection = _normalize_collection(case.get("collection"))
        row: dict[str, Any] = {
            "id": case.get("id") or query,
            "query": query,
            "collection": collection or "all",
            "expected_content": case.get("expected_content"),
            "expected_source": case.get("expected_source"),
            "rewrite_source": rewrite_source,
            "correction_attempts": [],
            "error": "",
        }
        try:
            initial, initial_results = _attempt(query, case, collection, top_k)
            row["initial"] = initial
            if initial["ok"]:
                row.update(
                    {
                        "recovery_needed": False,
                        "recovered": True,
                        "best_query": query,
                        "best_latency_ms": initial["latency_ms"],
                    }
                )
            else:
                row["recovery_needed"] = True
                best: dict[str, Any] | None = None
                correction_specs = _correction_queries_for_case(
                    case=case,
                    query=query,
                    initial_results=initial_results,
                    rewrite_source=rewrite_source,
                    llm_timeout_s=llm_timeout_s,
                )
                for spec in correction_specs:
                    correction_query = str(spec.get("query") or "").strip()
                    if not correction_query:
                        continue
                    attempt, _ = _attempt(correction_query, case, collection, top_k)
                    attempt.update({k: v for k, v in spec.items() if k != "query"})
                    row["correction_attempts"].append(attempt)
                    if best is None or (attempt["ok"] and not best["ok"]):
                        best = attempt
                    if attempt["ok"]:
                        break
                recovered = bool(best and best["ok"])
                row.update(
                    {
                        "recovered": recovered,
                        "best_query": best.get("query") if best else None,
                        "best_latency_ms": best.get("latency_ms") if best else None,
                    }
                )
        except Exception as exc:
            row.update(
                {
                    "initial": {"ok": False, "latency_ms": 0, "result_count": 0},
                    "recovery_needed": True,
                    "recovered": False,
                    "best_query": None,
                    "best_latency_ms": None,
                    "error": str(exc)[:200],
                }
            )
        rows.append(row)

    total = len(rows)
    recovery_needed = sum(1 for r in rows if r.get("recovery_needed"))
    recovered = sum(1 for r in rows if r.get("recovery_needed") and r.get("recovered"))
    already_ok = sum(1 for r in rows if not r.get("recovery_needed") and r.get("recovered"))
    failed_recoveries = recovery_needed - recovered
    recovery_rate = round((recovered / recovery_needed) * 100.0, 2) if recovery_needed else 100.0
    recovery_latencies = [
        int(r.get("best_latency_ms") or 0)
        for r in rows
        if r.get("recovery_needed") and r.get("best_latency_ms") is not None
    ]
    rewrite_latencies = [
        int(a.get("rewrite_latency_ms") or 0)
        for r in rows
        for a in r.get("correction_attempts", [])
        if a.get("rewrite_latency_ms") is not None
    ]
    mean_recovery_latency_ms = (
        round(sum(recovery_latencies) / len(recovery_latencies), 2) if recovery_latencies else 0.0
    )
    mean_rewrite_latency_ms = (
        round(sum(rewrite_latencies) / len(rewrite_latencies), 2) if rewrite_latencies else 0.0
    )
    status = "ok"
    if recovery_needed < min_recovery_cases:
        status = "insufficient_coverage"
    elif recovery_rate < min_recovery_rate or mean_recovery_latency_ms > max_mean_latency_ms:
        status = "breached"

    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "status": status,
        "eval_set": str(eval_set),
        "rewrite_source": rewrite_source,
        "top_k": top_k,
        "total": total,
        "already_ok": already_ok,
        "recovery_needed": recovery_needed,
        "recovered": recovered,
        "failed_recoveries": failed_recoveries,
        "recovery_rate": recovery_rate,
        "min_recovery_rate": min_recovery_rate,
        "min_recovery_cases": min_recovery_cases,
        "mean_recovery_latency_ms": mean_recovery_latency_ms,
        "mean_rewrite_latency_ms": mean_rewrite_latency_ms,
        "max_mean_latency_ms": max_mean_latency_ms,
        "duration_s": round(time.time() - started, 3),
        "rows": rows,
    }
    report_file = LLM_REPORT_FILE if rewrite_source == "llm" else REPORT_FILE
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eval-set", type=Path, default=DEFAULT_EVAL_SET)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--rewrite-source", choices=("deterministic", "llm"), default="deterministic")
    parser.add_argument("--llm-timeout-s", type=float, default=8.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = run(
        args.eval_set,
        limit=args.limit,
        top_k=args.top_k,
        rewrite_source=args.rewrite_source,
        llm_timeout_s=args.llm_timeout_s,
    )
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(
            f"crag correction ({report['rewrite_source']}): "
            f"status={report['status']} recovery={report['recovery_rate']}% "
            f"cases={report['recovered']}/{report['recovery_needed']}"
        )
    # 2026-05-19: exit 0 unconditionally. status flows through
    # ops_readiness.crag_correction_regression_snapshot into the SLO
    # of the same name; double-counting as scheduler_failures masks real
    # job-runtime errors.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
