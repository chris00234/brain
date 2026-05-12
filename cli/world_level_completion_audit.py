#!/usr/bin/env python3
"""Prompt-to-artifact completion audit for the world-level Brain goal.

This is a guardrail, not a completion claim. It reads the active audit ledger's
prompt-to-artifact checklist, classifies each row as pass/weak/open, verifies
that key evidence artifacts exist, and emits JSON. Use ``--fail-on-open`` in CI
or before any final completion claim.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT_DOC = ROOT / "docs" / "world-level-brain-audit-2026-05-05.md"
PRD_PATH = ROOT / ".omx" / "plans" / "prd-world-level-brain-hardening.md"
TEST_SPEC_PATH = ROOT / ".omx" / "plans" / "test-spec-world-level-brain-hardening.md"

PASS_TERMS = (
    "active constraint",
    "implemented",
    "covered",
    "green",
    "passed",
    "ok",
)
WEAK_TERMS = (
    "covered for first pass",
    "first pass",
    "ongoing",
    "continue",
    "statistically useful",
    "remaining gate",
)
OPEN_TERMS = (
    "not complete",
    "in progress",
    "weakly verified",
    "missing",
    "blocked",
    "todo",
)


@dataclass(frozen=True)
class ChecklistRow:
    requirement: str
    evidence: str
    raw_status: str


def _strip_cell(cell: str) -> str:
    return re.sub(r"\s+", " ", cell.strip().strip("|")).strip()


def parse_prompt_checklist(markdown: str) -> list[ChecklistRow]:
    """Extract rows from the prompt-to-artifact markdown table."""

    marker = "## Prompt-to-artifact checklist"
    start = markdown.find(marker)
    if start < 0:
        return []
    rest = markdown[start + len(marker) :]
    next_heading = rest.find("\n## ")
    if next_heading >= 0:
        rest = rest[:next_heading]

    rows: list[ChecklistRow] = []
    for line in rest.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "|" not in stripped[1:]:
            continue
        cells = [_strip_cell(c) for c in stripped.strip("|").split("|")]
        if len(cells) < 3:
            continue
        first = cells[0].lower()
        if first == "requirement" or set(first.replace(" ", "")) <= {"-"}:
            continue
        if set("".join(cells).replace(" ", "")) <= {"-", ":"}:
            continue
        rows.append(ChecklistRow(cells[0], cells[1], cells[2]))
    return rows


def classify_status(status: str) -> str:
    lowered = status.lower()
    if any(term in lowered for term in OPEN_TERMS):
        return "open"
    if any(term in lowered for term in WEAK_TERMS):
        return "weak"
    if any(term in lowered for term in PASS_TERMS):
        return "pass"
    return "weak"


def artifact_checks(root: Path = ROOT) -> list[dict]:
    """Check for concrete artifacts required by the PRD/test spec."""

    required = [
        ("audit_doc", AUDIT_DOC.relative_to(root), "prompt-to-artifact ledger"),
        ("prd", PRD_PATH.relative_to(root), "requirements artifact"),
        ("test_spec", TEST_SPEC_PATH.relative_to(root), "validation plan"),
        (
            "dispatch_contract_tests",
            Path("tests/unit/test_cli_first_dispatch_contract.py"),
            "CLI-first regression lock",
        ),
        (
            "dispatch_truth_tests",
            Path("tests/unit/test_task_dispatch_attempts.py"),
            "execution-truth/task-eval tests",
        ),
        ("source_governance", Path("brain_core/source_governance.py"), "ingestion governance gate"),
        ("privacy_negative_audit", Path("cli/privacy_negative_audit.py"), "privacy-negative gate"),
        ("ui_parity_audit", Path("cli/ui_parity_audit.py"), "backend/UI parity gate"),
        (
            "readiness_surface_manifest",
            Path("brain_core/readiness_surface_manifest.py"),
            "readiness field manifest",
        ),
        (
            "readiness_surface_manifest_tests",
            Path("tests/unit/test_readiness_surface_manifest.py"),
            "readiness manifest regression tests",
        ),
        ("crag_regression", Path("cli/crag_regression.py"), "retrieval confidence gate"),
        ("ragas_judge_tests", Path("tests/unit/test_ragas_judge.py"), "CLI-first RAGAS judge test"),
        (
            "ragas_eval_set_audit",
            Path("cli/ragas_eval_set_audit.py"),
            "generated-answer RAGAS eval-set coverage audit",
        ),
        (
            "ragas_eval_set_audit_tests",
            Path("tests/unit/test_ragas_eval_set_audit.py"),
            "RAGAS eval-set coverage tests",
        ),
        ("world_level_bug_audit", Path("cli/world_level_bug_audit.py"), "executable bug-fix ledger"),
        (
            "world_level_bug_audit_tests",
            Path("tests/unit/test_world_level_bug_audit.py"),
            "bug audit regression tests",
        ),
        (
            "world_level_gap_audit",
            Path("cli/world_level_gap_audit.py"),
            "modification/improvement backlog audit",
        ),
        (
            "world_level_gap_audit_tests",
            Path("tests/unit/test_world_level_gap_audit.py"),
            "gap audit regression tests",
        ),
        (
            "research_refresh",
            Path("docs/research/world-level-brain-research-refresh-2026-05-05.md"),
            "current primary-source research/repo refresh",
        ),
        (
            "research_refresh_2026_05_07",
            Path("docs/research/world-level-brain-research-refresh-2026-05-07.md"),
            "latest xMemory/agent-memory research refresh",
        ),
        (
            "eval_diversity_metrics",
            Path("cli/eval_compare.py"),
            "existing eval pipeline final-top-k diversity diagnostics",
        ),
        (
            "eval_diversity_tests",
            Path("tests/unit/test_eval_compare_source.py"),
            "diversity diagnostic regression tests",
        ),
        (
            "eval_diversity_sample",
            Path("logs/eval-diversity-sample-2026-05-07.json"),
            "live diversity metric sample",
        ),
    ]
    out = []
    for key, rel, description in required:
        path = root / rel
        out.append(
            {
                "key": key,
                "path": str(rel),
                "description": description,
                "exists": path.exists(),
                "status": "pass" if path.exists() else "open",
            }
        )
    return out


def run(audit_doc: Path = AUDIT_DOC, root: Path = ROOT) -> dict:
    markdown = audit_doc.read_text() if audit_doc.exists() else ""
    rows = parse_prompt_checklist(markdown)
    classified_rows = [
        {
            "requirement": row.requirement,
            "evidence": row.evidence,
            "raw_status": row.raw_status,
            "status": classify_status(row.raw_status),
        }
        for row in rows
    ]
    artifacts = artifact_checks(root)
    counts = {
        "pass": sum(1 for row in classified_rows if row["status"] == "pass"),
        "weak": sum(1 for row in classified_rows if row["status"] == "weak"),
        "open": sum(1 for row in classified_rows if row["status"] == "open"),
        "artifact_missing": sum(1 for item in artifacts if not item["exists"]),
    }
    completion_ready = (
        bool(rows) and counts["open"] == 0 and counts["weak"] == 0 and counts["artifact_missing"] == 0
    )
    return {
        "status": "ready_for_final_review" if completion_ready else "not_ready",
        "completion_ready": completion_ready,
        "audit_doc": str(audit_doc.relative_to(root) if audit_doc.is_relative_to(root) else audit_doc),
        "row_count": len(classified_rows),
        "counts": counts,
        "rows": classified_rows,
        "artifacts": artifacts,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="World-level Brain prompt-to-artifact completion audit")
    parser.add_argument("--json", action="store_true", help="Emit JSON only")
    parser.add_argument("--fail-on-open", action="store_true", help="Exit 1 unless completion_ready is true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    report = run()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"status={report['status']} completion_ready={report['completion_ready']}")
        print(f"counts={json.dumps(report['counts'], sort_keys=True)}")
        for row in report["rows"]:
            if row["status"] != "pass":
                print(f"{row['status'].upper()}: {row['requirement']} — {row['raw_status']}")
    if args.fail_on_open and not report["completion_ready"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
