#!/usr/bin/env python3
"""Lightweight retrieval regression gate for chunking/tagging/index changes.

Runs a bounded subset of the stable eval set through search_unified and checks
whether expected source/content appears in top-k. The report is JSON and is
safe for dashboards; it contains only query/result metadata snippets already in
Brain retrieval outputs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BRAIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))

REPORT_FILE = BRAIN_ROOT / "logs" / "retrieval_regression.json"
DEFAULT_EVAL_SET = BRAIN_ROOT / "cli" / "eval_set_stable.json"
DEFAULT_MIN_PASS_RATE = 80.0
QUALITY_GATE_CATEGORIES = frozenset(
    {
        "stale_fact_supersession",
        "privacy_negative_personal_source",
        "identity_canon_over_stale_provenance",
        "clean_hit_topk_noise",
    }
)


def _min_pass_rate() -> float:
    raw = os.getenv("BRAIN_RETRIEVAL_REGRESSION_MIN_PASS_RATE")
    if not raw:
        return DEFAULT_MIN_PASS_RATE
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_MIN_PASS_RATE
    return min(max(value, 0.0), 100.0)


def _load_cases(path: Path, limit: int) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("eval_set_root_not_list")
    cases = [r for r in data if isinstance(r, dict) and r.get("query")]
    selected = list(cases[:limit])
    if path.resolve() == DEFAULT_EVAL_SET.resolve():
        seen = {str(case.get("query") or "") for case in selected}
        selected.extend(
            case
            for case in cases[limit:]
            if case.get("category") in QUALITY_GATE_CATEGORIES and str(case.get("query") or "") not in seen
        )
    return selected


def _normalize_collection(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    if not normalized or normalized.lower() in {"all", "*"}:
        return None
    return normalized


def _result_text(result: Any) -> str:
    if isinstance(result, dict):
        parts = [result.get(k) for k in ("title", "content", "path", "source", "collection", "source_type")]
        meta = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        parts.extend(meta.get(k) for k in ("path", "source", "source_type", "title"))
        return " ".join(str(p) for p in parts if p).lower()
    return str(result).lower()


def _expected_hits(case: dict[str, Any], haystack: str) -> tuple[bool, bool, bool, bool]:
    expected_content = str(case.get("expected_content") or "").lower().strip()
    expected_source = str(case.get("expected_source") or "").lower().strip()
    alternates_raw = case.get("expected_alternates") or []
    alternates = [str(v).lower().strip() for v in alternates_raw if str(v).strip()]
    forbidden_raw = case.get("forbidden_content") or []
    forbidden = [str(v).lower().strip() for v in forbidden_raw if str(v).strip()]
    content_hit = bool(expected_content and expected_content in haystack)
    source_hit = bool(expected_source and expected_source in haystack)
    alternate_hit = any(alt in haystack for alt in alternates)
    forbidden_hit = any(term in haystack for term in forbidden)
    return content_hit, source_hit, alternate_hit, forbidden_hit


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


def run(eval_set: Path = DEFAULT_EVAL_SET, *, limit: int = 20, top_k: int = 5) -> dict[str, Any]:
    started = time.time()
    rows = []
    cases = _load_cases(eval_set, limit)
    for case in cases:
        query = str(case.get("query") or "")
        expected_content = str(case.get("expected_content") or "").lower().strip()
        expected_source = str(case.get("expected_source") or "").lower().strip()
        collection = _normalize_collection(case.get("collection"))
        t0 = time.time()
        try:
            results = _search(query, collection, top_k)
            haystack = "\n".join(_result_text(r) for r in results)
            content_hit, source_hit, alternate_hit, forbidden_hit = _expected_hits(case, haystack)
            ok = (content_hit or source_hit or alternate_hit) and not forbidden_hit
            error = ""
        except Exception as exc:
            results = []
            content_hit = False
            source_hit = False
            alternate_hit = False
            forbidden_hit = False
            ok = False
            error = str(exc)[:200]
        rows.append(
            {
                "query": query,
                "collection": collection or "all",
                "expected_content": expected_content,
                "expected_source": expected_source,
                "content_hit": content_hit,
                "source_hit": source_hit,
                "alternate_hit": alternate_hit,
                "forbidden_hit": forbidden_hit,
                "ok": ok,
                "result_count": len(results),
                "latency_ms": int((time.time() - t0) * 1000),
                "error": error,
            }
        )
    passed = sum(1 for r in rows if r["ok"])
    total = len(rows)
    pass_rate = round((passed / total) * 100.0, 2) if total else 100.0
    min_pass_rate = _min_pass_rate()
    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "status": "ok" if pass_rate >= min_pass_rate else "breached",
        "eval_set": str(eval_set),
        "limit": limit,
        "top_k": top_k,
        "min_pass_rate": min_pass_rate,
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": pass_rate,
        "duration_s": round(time.time() - started, 3),
        "rows": rows,
    }
    REPORT_FILE.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eval-set", type=Path, default=DEFAULT_EVAL_SET)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = run(args.eval_set, limit=args.limit, top_k=args.top_k)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"retrieval regression: {report['passed']}/{report['total']} pass ({report['pass_rate']}%)")
    # 2026-05-19: exit 0 unconditionally. status="breached" already flows
    # through ops_readiness.retrieval_regression_snapshot into the SLO
    # `retrieval_regression`, which is the proper alerting surface.
    # Failing the job too double-counts the breach as both an SLO and a
    # scheduler_failures entry, masking real job-runtime errors (those
    # would surface as non-zero exits before the report writes).
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
