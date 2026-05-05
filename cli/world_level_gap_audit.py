#!/usr/bin/env python3
"""Executable backlog/gap audit for world-level Brain readiness.

This audit does not claim the Brain is world-level. It verifies that the needed
modifications and broader improvement opportunities have been identified in a
prioritized, evidence-backed backlog with implemented/current/remaining gates.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT_DOC = ROOT / "docs" / "world-level-brain-audit-2026-05-05.md"
REPORT_FILE = ROOT / "logs" / "world-level-gap-audit.json"


@dataclass(frozen=True)
class Theme:
    id: str
    label: str
    tokens: tuple[str, ...]


THEMES: tuple[Theme, ...] = (
    Theme(
        "execution_truth",
        "trust and execution truth",
        ("Agent execution truth ledger", "task_dispatch_attempts"),
    ),
    Theme(
        "dispatch_health",
        "dispatch health and breaker semantics",
        ("Dispatch health SLO", "Breaker semantics audit", "failure_taxonomy"),
    ),
    Theme(
        "retrieval_eval",
        "retrieval and answer evaluation",
        (
            "CRAG-style retrieval evaluator",
            "RAGAS-like nightly regression",
            "Adversarial memory evals",
            "ragas_eval_set_audit",
        ),
    ),
    Theme(
        "skill_learning",
        "skill/procedure learning",
        ("Voyager/AWM-style skill promotion", "Reflexion-style failure lessons"),
    ),
    Theme(
        "architecture_efficiency",
        "architecture consolidation and resource efficiency",
        ("Large-module split", "resource efficiency", "active_recall.py"),
    ),
    Theme(
        "ui_observability",
        "FastAPI/UI observability",
        (
            "FastAPI/UI parity map",
            "Brain UI Observability",
            "ui_parity_audit",
            "readiness-surface-manifest-v1",
        ),
    ),
    Theme(
        "ingestion_governance",
        "high-value ingestion governance",
        ("High-value ingestion governance", "source_governance", "privacy_negative_audit"),
    ),
)


@dataclass(frozen=True)
class Gate:
    id: str
    label: str
    ok: bool
    detail: str


def _section(markdown: str, heading: str) -> str:
    start = markdown.find(heading)
    if start < 0:
        return ""
    rest = markdown[start + len(heading) :]
    next_heading = rest.find("\n## ")
    return rest[:next_heading] if next_heading >= 0 else rest


def _theme_row(theme: Theme, text: str) -> dict:
    missing = [token for token in theme.tokens if token not in text]
    return {**asdict(theme), "status": "pass" if not missing else "blocked", "missing_tokens": missing}


def run(*, audit_doc: Path = AUDIT_DOC, report_file: Path = REPORT_FILE, write_report: bool = True) -> dict:
    markdown = audit_doc.read_text(encoding="utf-8") if audit_doc.exists() else ""
    backlog = _section(markdown, "## Modification backlog toward world-level readiness")
    priority_sections = re.findall(r"^###\s+(P\d)\s+—\s+(.+)$", backlog, flags=re.MULTILINE)
    numbered_items = re.findall(r"^\d+\.\s+\*\*(.+?)\*\*", backlog, flags=re.MULTILINE)
    implemented_count = backlog.count("Implemented now:")
    remaining_gate_count = backlog.count("Remaining gate:")
    next_count = backlog.count("Next:")
    live_evidence_count = len(re.findall(r"Live (?:evidence|gate)|Current live", backlog))
    themes = [_theme_row(theme, backlog) for theme in THEMES]

    gates = [
        Gate(
            "doc_exists",
            "audit document exists",
            audit_doc.exists(),
            str(audit_doc.relative_to(ROOT) if audit_doc.exists() else audit_doc),
        ),
        Gate(
            "backlog_section",
            "modification backlog section exists",
            bool(backlog.strip()),
            "## Modification backlog toward world-level readiness",
        ),
        Gate(
            "priority_coverage",
            "P0/P1/P2 priority coverage",
            {p for p, _ in priority_sections} >= {"P0", "P1", "P2"},
            str(priority_sections),
        ),
        Gate(
            "item_count",
            "at least ten concrete backlog items",
            len(numbered_items) >= 10,
            f"items={len(numbered_items)}",
        ),
        Gate(
            "implemented_evidence",
            "implemented/current fixes are recorded",
            implemented_count >= 20,
            f"implemented_now={implemented_count}",
        ),
        Gate(
            "remaining_gates",
            "remaining gates are explicitly identified",
            remaining_gate_count >= 6,
            f"remaining_gate={remaining_gate_count}",
        ),
        Gate(
            "next_steps", "near-term next modifications are identified", next_count >= 2, f"next={next_count}"
        ),
        Gate(
            "live_evidence",
            "live/current evidence anchors exist",
            live_evidence_count >= 5,
            f"live_evidence={live_evidence_count}",
        ),
        Gate(
            "theme_coverage",
            "broader improvement themes are covered",
            all(row["status"] == "pass" for row in themes),
            "themes=" + str(len(themes)),
        ),
    ]
    rows = [asdict(gate) | {"status": "pass" if gate.ok else "blocked"} for gate in gates]
    blocked = [row for row in rows if row["status"] != "pass"]
    blocked_themes = [row for row in themes if row["status"] != "pass"]
    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "status": "ok" if not blocked and not blocked_themes else "blocked",
        "modification_items": len(numbered_items),
        "implemented_evidence_count": implemented_count,
        "remaining_gate_count": remaining_gate_count,
        "next_step_count": next_count,
        "live_evidence_count": live_evidence_count,
        "priority_sections": [{"priority": p, "label": label} for p, label in priority_sections],
        "identified_modifications": numbered_items,
        "theme_count": len(themes),
        "themes": themes,
        "gates": rows,
        "content_suppressed": True,
    }
    if write_report:
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="World-level Brain modification/improvement backlog audit")
    parser.add_argument("--json", action="store_true", help="Emit JSON only")
    parser.add_argument(
        "--fail-on-blocked", action="store_true", help="Exit 1 if backlog coverage is blocked"
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    report = run()
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(
            "status={status} modifications={mods} themes={themes} remaining_gates={remaining}".format(
                status=report["status"],
                mods=report["modification_items"],
                themes=report["theme_count"],
                remaining=report["remaining_gate_count"],
            )
        )
        for row in report["gates"]:
            if row["status"] != "pass":
                print(f"BLOCKED: {row['id']} — {row['detail']}")
        for row in report["themes"]:
            if row["status"] != "pass":
                print(f"BLOCKED_THEME: {row['id']} — missing {row['missing_tokens']}")
    return 1 if args.fail_on_blocked and report["status"] != "ok" else 0


if __name__ == "__main__":
    raise SystemExit(main())
