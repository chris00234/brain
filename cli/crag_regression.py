#!/usr/bin/env python3
"""CRAG retrieval-evaluator regression gate.

This is the cheap, non-LLM guard for the Corrective RAG path. It reuses the
stable eval rows, runs live retrieval, scores the returned window with
``brain_core.crag.score_confidence()``, and verifies the evaluator would not
silently accept a bad non-empty result set.

It does not run the expensive rewrite hop by default; the readiness invariant is
narrower and safer: when retrieval is wrong or weak, the confidence gate must
choose a corrective/fallback path instead of accepting misleading context.
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

REPORT_FILE = BRAIN_ROOT / "logs" / "crag_regression.json"
DEFAULT_EVAL_SET = BRAIN_ROOT / "cli" / "eval_set_stable.json"
DEFAULT_MIN_SAFETY_RATE = 100.0
DEFAULT_MAX_CORRECTIVE_TRIGGER_RATE = 65.0
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


def _load_cases(path: Path, limit: int) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("eval_set_root_not_list")
    cases = [r for r in data if isinstance(r, dict) and r.get("query")]
    return cases[:limit]


def _result_text(result: Any) -> str:
    if isinstance(result, dict):
        parts = [result.get(k) for k in ("title", "content", "path", "source", "collection", "source_type")]
        meta = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        parts.extend(meta.get(k) for k in ("path", "source", "source_type", "title", "category", "domain"))
        return " ".join(str(p) for p in parts if p).lower()
    return str(result).lower()


def _expected_hits(case: dict[str, Any], haystack: str) -> tuple[bool, bool, bool]:
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


def run(eval_set: Path = DEFAULT_EVAL_SET, *, limit: int = 40, top_k: int = DEFAULT_TOP_K) -> dict[str, Any]:
    from crag import score_confidence, should_iterate

    started = time.time()
    min_safety_rate = _env_float("BRAIN_CRAG_MIN_SAFETY_RATE", DEFAULT_MIN_SAFETY_RATE)
    max_trigger_rate = _env_float(
        "BRAIN_CRAG_MAX_CORRECTIVE_TRIGGER_RATE", DEFAULT_MAX_CORRECTIVE_TRIGGER_RATE
    )
    rows: list[dict[str, Any]] = []
    cases = _load_cases(eval_set, limit)
    for case in cases:
        query = str(case.get("query") or "")
        collection = _normalize_collection(case.get("collection"))
        t0 = time.time()
        try:
            results = _search(query, collection, top_k)
            haystack = "\n".join(_result_text(r) for r in results)
            content_hit, source_hit, alternate_hit = _expected_hits(case, haystack)
            ok = content_hit or source_hit or alternate_hit
            report = score_confidence([r for r in results if isinstance(r, dict)], query=query)
            iterate = should_iterate(report)
            empty_miss = (not ok) and report.n_results == 0
            dangerous_false_accept = (not ok) and report.n_results > 0 and not iterate
            overcorrection = ok and iterate
            corrective_candidate = empty_miss or iterate
            action = "external_needed" if empty_miss else ("correct" if iterate else "accept")
            error = ""
        except Exception as exc:
            results = []
            content_hit = False
            source_hit = False
            alternate_hit = False
            ok = False
            report = score_confidence([])
            iterate = False
            empty_miss = False
            dangerous_false_accept = True
            overcorrection = False
            corrective_candidate = False
            action = "error"
            error = str(exc)[:200]
        rows.append(
            {
                "query": query,
                "collection": collection or "all",
                "ok": ok,
                "content_hit": content_hit,
                "source_hit": source_hit,
                "alternate_hit": alternate_hit,
                "result_count": len(results),
                "confidence": report.score,
                "top_score": report.top_score,
                "score_spread": report.score_spread,
                "confidence_components": report.components,
                "should_iterate": iterate,
                "action": action,
                "corrective_candidate": corrective_candidate,
                "dangerous_false_accept": dangerous_false_accept,
                "empty_miss": empty_miss,
                "overcorrection": overcorrection,
                "latency_ms": int((time.time() - t0) * 1000),
                "error": error,
            }
        )

    total = len(rows)
    dangerous_false_accepts = sum(1 for r in rows if r["dangerous_false_accept"])
    empty_misses = sum(1 for r in rows if r["empty_miss"])
    overcorrections = sum(1 for r in rows if r["overcorrection"])
    corrective_candidates = sum(1 for r in rows if r["corrective_candidate"])
    safety_rate = round(((total - dangerous_false_accepts) / total) * 100.0, 2) if total else 100.0
    corrective_trigger_rate = round((corrective_candidates / total) * 100.0, 2) if total else 0.0
    ok_rows = sum(1 for r in rows if r["ok"])
    failed_rows = total - ok_rows
    status = "ok"
    if safety_rate < min_safety_rate or corrective_trigger_rate > max_trigger_rate:
        status = "breached"

    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "status": status,
        "eval_set": str(eval_set),
        "limit": limit,
        "top_k": top_k,
        "min_safety_rate": min_safety_rate,
        "max_corrective_trigger_rate": max_trigger_rate,
        "total": total,
        "ok_rows": ok_rows,
        "failed_rows": failed_rows,
        "safety_rate": safety_rate,
        "corrective_trigger_rate": corrective_trigger_rate,
        "dangerous_false_accepts": dangerous_false_accepts,
        "empty_misses": empty_misses,
        "overcorrections": overcorrections,
        "corrective_candidates": corrective_candidates,
        "duration_s": round(time.time() - started, 3),
        "rows": rows,
    }
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eval-set", type=Path, default=DEFAULT_EVAL_SET)
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = run(args.eval_set, limit=args.limit, top_k=args.top_k)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(
            "crag regression: "
            f"safety={report['safety_rate']}% false_accepts={report['dangerous_false_accepts']} "
            f"trigger={report['corrective_trigger_rate']}%"
        )
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
