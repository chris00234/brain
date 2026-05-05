from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "cli"))

import world_level_gap_audit as audit  # noqa: E402


def test_world_level_gap_audit_covers_modifications_and_improvements(tmp_path):
    report = audit.run(report_file=tmp_path / "gap-audit.json")

    assert report["status"] == "ok"
    assert report["modification_items"] >= 10
    assert report["implemented_evidence_count"] >= 20
    assert report["remaining_gate_count"] >= 6
    assert report["next_step_count"] >= 2
    assert report["live_evidence_count"] >= 5
    assert report["theme_count"] >= 7
    assert report["content_suppressed"] is True
    assert all(row["status"] == "pass" for row in report["gates"])
    assert all(row["status"] == "pass" for row in report["themes"])
    assert (tmp_path / "gap-audit.json").exists()

    theme_ids = {row["id"] for row in report["themes"]}
    assert {
        "execution_truth",
        "retrieval_eval",
        "skill_learning",
        "architecture_efficiency",
        "ui_observability",
        "ingestion_governance",
    } <= theme_ids


def test_section_parser_stops_at_next_heading():
    md = """
## Modification backlog toward world-level readiness
inside
## Current verification evidence
outside
"""

    assert audit._section(md, "## Modification backlog toward world-level readiness") == "\ninside"


def test_main_fail_on_blocked_returns_zero_for_current_gap_audit(capsys):
    code = audit.main(["--fail-on-blocked"])
    out = capsys.readouterr().out

    assert code == 0
    assert "status=ok" in out
