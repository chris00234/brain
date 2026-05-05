#!/usr/bin/env python3
"""Static audit for generated-answer RAGAS eval-set coverage."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVAL_SET = ROOT / "cli" / "eval_set_ragas_answers.json"
JOB_REGISTRY = ROOT / "brain_core" / "job_registry.py"
REPORT_FILE = ROOT / "logs" / "ragas-eval-set-audit.json"
MIN_CASES = 12
REQUIRED_CATEGORIES = {
    "anti_false_success",
    "stale_fact_supersession",
    "multilingual_mixed_recall",
    "agent_handoff_state",
    "privacy_negative_personal_source",
    "cli_first_task_eval",
    "source_governance_privacy",
    "failure_lesson_outcome",
    "ui_readiness_manifest",
}
REQUIRED_FIELDS = (
    "query",
    "expected_source",
    "expected_content",
    "category",
    "purpose",
    "answer_rubric",
)


def _load_cases(path: Path = EVAL_SET) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def run(*, eval_set: Path = EVAL_SET, report_file: Path = REPORT_FILE, write_report: bool = True) -> dict:
    cases = _load_cases(eval_set)
    missing_fields = []
    weak_rubrics = []
    non_ragas = []
    for i, case in enumerate(cases, start=1):
        for field in REQUIRED_FIELDS:
            if not case.get(field):
                missing_fields.append(
                    {"index": i, "field": field, "query": str(case.get("query") or "")[:120]}
                )
        if len(str(case.get("answer_rubric") or "")) < 40:
            weak_rubrics.append({"index": i, "query": str(case.get("query") or "")[:120]})
        if case.get("ragas_answer_eval") is not True:
            non_ragas.append({"index": i, "query": str(case.get("query") or "")[:120]})
    categories = {str(case.get("category") or "") for case in cases}
    duplicate_queries = [
        query for query, count in Counter(str(c.get("query") or "") for c in cases).items() if count > 1
    ]
    job_text = JOB_REGISTRY.read_text(encoding="utf-8", errors="ignore") if JOB_REGISTRY.exists() else ""
    gates = [
        {"id": "min_cases", "ok": len(cases) >= MIN_CASES, "detail": f"cases={len(cases)} min={MIN_CASES}"},
        {
            "id": "required_categories",
            "ok": categories >= REQUIRED_CATEGORIES,
            "detail": sorted(REQUIRED_CATEGORIES - categories),
        },
        {"id": "required_fields", "ok": not missing_fields, "detail": missing_fields[:20]},
        {"id": "answer_rubrics", "ok": not weak_rubrics, "detail": weak_rubrics[:20]},
        {"id": "ragas_answer_eval", "ok": not non_ragas, "detail": non_ragas[:20]},
        {"id": "duplicate_queries", "ok": not duplicate_queries, "detail": duplicate_queries[:20]},
        {
            "id": "scheduled_job_uses_eval_set",
            "ok": "eval_set_ragas_answers.json" in job_text and "--ragas-answer-source" in job_text,
            "detail": "brain_core/job_registry.py",
        },
    ]
    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "status": "ok" if all(gate["ok"] for gate in gates) else "blocked",
        "eval_set": str(eval_set.relative_to(ROOT) if eval_set.is_relative_to(ROOT) else eval_set),
        "case_count": len(cases),
        "min_cases": MIN_CASES,
        "category_count": len(categories),
        "required_categories": sorted(REQUIRED_CATEGORIES),
        "categories": sorted(categories),
        "gates": [{**gate, "status": "pass" if gate["ok"] else "blocked"} for gate in gates],
        "content_suppressed": True,
    }
    if write_report:
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit generated-answer RAGAS eval-set coverage")
    parser.add_argument("--json", action="store_true", help="Emit JSON only")
    parser.add_argument("--fail-on-blocked", action="store_true", help="Exit 1 if coverage is blocked")
    args = parser.parse_args(list(argv) if argv is not None else None)
    report = run()
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"status={report['status']} cases={report['case_count']} categories={report['category_count']}")
        for gate in report["gates"]:
            if gate["status"] != "pass":
                print(f"BLOCKED: {gate['id']} — {gate['detail']}")
    return 1 if args.fail_on_blocked and report["status"] != "ok" else 0


if __name__ == "__main__":
    raise SystemExit(main())
