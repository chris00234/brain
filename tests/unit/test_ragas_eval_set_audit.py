from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "cli"))

import ragas_eval_set_audit as audit  # noqa: E402


def test_ragas_eval_set_audit_current_pack_is_broad_and_rubriced(tmp_path):
    report = audit.run(report_file=tmp_path / "ragas-audit.json")

    assert report["status"] == "ok"
    assert report["case_count"] >= 12
    assert report["category_count"] >= 9
    assert report["content_suppressed"] is True
    assert all(gate["status"] == "pass" for gate in report["gates"])
    assert "cli_first_task_eval" in report["categories"]
    assert "source_governance_privacy" in report["categories"]
    assert "failure_lesson_outcome" in report["categories"]
    assert "ui_readiness_manifest" in report["categories"]
    assert (tmp_path / "ragas-audit.json").exists()


def test_ragas_eval_set_audit_main_fail_on_blocked_is_green(capsys):
    code = audit.main(["--fail-on-blocked"])
    out = capsys.readouterr().out

    assert code == 0
    assert "status=ok" in out
