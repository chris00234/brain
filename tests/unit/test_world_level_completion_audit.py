from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "cli"))

import world_level_completion_audit as audit  # noqa: E402


def test_parse_prompt_checklist_and_classify_statuses():
    md = """
## Prompt-to-artifact checklist

| Requirement | Current evidence | Status |
| --- | --- | --- |
| Search papers | Some table | Covered for first pass; continue later. |
| Fix bugs | Tests passed | Active constraint. |
| Finish | Not yet | Not complete. |

## Next
"""
    rows = audit.parse_prompt_checklist(md)

    assert [row.requirement for row in rows] == ["Search papers", "Fix bugs", "Finish"]
    assert [audit.classify_status(row.raw_status) for row in rows] == ["weak", "pass", "open"]


def test_current_completion_audit_is_ready_for_final_review_with_required_artifacts():
    report = audit.run()

    assert report["status"] == "ready_for_final_review"
    assert report["completion_ready"] is True
    assert report["row_count"] >= 7
    assert report["counts"]["open"] == 0
    assert report["counts"]["weak"] == 0
    assert report["counts"]["artifact_missing"] == 0
    by_req = {row["requirement"]: row for row in report["rows"]}
    assert by_req["Work until world-level ready"]["status"] == "pass"
    assert by_req["Search related research papers"]["status"] == "pass"
    assert by_req["Search related GitHub repos"]["status"] == "pass"
    assert by_req["Find bugs"]["status"] == "pass"
    assert by_req["Find modifications needed"]["status"] == "pass"
    assert by_req["Find improvements possible"]["status"] == "pass"

    by_artifact = {item["key"]: item for item in report["artifacts"]}
    assert by_artifact["world_level_bug_audit"]["status"] == "pass"
    assert by_artifact["world_level_bug_audit_tests"]["status"] == "pass"
    assert by_artifact["world_level_gap_audit"]["status"] == "pass"
    assert by_artifact["world_level_gap_audit_tests"]["status"] == "pass"
    assert by_artifact["readiness_surface_manifest"]["status"] == "pass"
    assert by_artifact["readiness_surface_manifest_tests"]["status"] == "pass"
    assert by_artifact["ragas_eval_set_audit"]["status"] == "pass"
    assert by_artifact["ragas_eval_set_audit_tests"]["status"] == "pass"


def test_main_fail_on_open_returns_zero_for_current_ready_audit(capsys):
    code = audit.main(["--fail-on-open"])
    out = capsys.readouterr().out

    assert code == 0
    assert "status=ready_for_final_review" in out
    assert "OPEN: Work until world-level ready" not in out


def test_research_refresh_artifact_contains_current_primary_sources():
    path = ROOT / "docs" / "research" / "world-level-brain-research-refresh-2026-05-05.md"
    text = path.read_text()

    for token in [
        "https://arxiv.org/abs/2504.19413",
        "https://github.com/mem0ai/mem0",
        "https://github.com/getzep/graphiti",
        "https://arxiv.org/abs/2502.12110",
        "https://github.com/BAI-LAB/MemoryOS",
        "https://github.com/OSU-NLP-Group/HippoRAG",
        "https://github.com/vectorize-io/hindsight",
    ]:
        assert token in text

    assert "Dependency adoption is not implied" in text
    assert "CLI/subscription budget contract" in text
