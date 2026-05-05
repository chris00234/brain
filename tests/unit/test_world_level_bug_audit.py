from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "cli"))

import world_level_bug_audit as audit  # noqa: E402


def test_world_level_bug_audit_locks_high_impact_bug_classes(tmp_path):
    report = audit.run(report_file=tmp_path / "bug-audit.json")

    assert report["status"] == "ok"
    assert report["bug_classes_checked"] >= 8
    assert report["bug_classes_locked"] == report["bug_classes_checked"]
    assert report["blocked"] == 0
    assert report["content_suppressed"] is True
    assert (tmp_path / "bug-audit.json").exists()

    by_id = {row["id"]: row for row in report["rows"]}
    for required in [
        "fake_automation_dispatch_truth",
        "openclaw_primary_bypass",
        "task_evaluation_alert_policy",
        "privacy_negative_payload_gate",
        "completion_truth_gap",
    ]:
        assert by_id[required]["status"] == "pass"
        assert by_id[required]["evidence_files"]


def test_evaluate_check_reports_missing_tokens_without_leaking_content(monkeypatch):
    check = audit.BugCheck(
        id="example",
        label="example",
        bug="bug",
        fix="fix",
        evidence=(audit.EvidenceToken("README.md", "definitely-not-present-token"),),
        forbidden=(audit.ForbiddenToken("README.md", "Brain", "forbidden test"),),
    )
    monkeypatch.setattr(audit, "ROOT", ROOT)

    row = audit.evaluate_check(check)

    assert row["status"] == "blocked"
    assert row["missing_tokens"] == [{"path": "README.md", "token": "definitely-not-present-token"}]
    assert row["forbidden_hits"] == [{"path": "README.md", "token": "Brain", "reason": "forbidden test"}]
    assert "content" not in row


def test_main_fail_on_blocked_returns_zero_for_current_locked_bug_audit(capsys):
    code = audit.main(["--fail-on-blocked"])
    out = capsys.readouterr().out

    assert code == 0
    assert "status=ok" in out
