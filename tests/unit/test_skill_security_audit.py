from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core"))

import skill_materializer
import skill_security_audit


def _profile_dirs(tmp_path: Path) -> dict[str, Path]:
    return {
        profile: tmp_path / ".hermes" / "profiles" / profile / "skills"
        for profile in skill_materializer.HERMES_PROFILE_NAMES
    }


def _write_auto_skill(root: Path, slug: str, body: str = "# ok\n") -> Path:
    d = root / slug
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\n"
        f"name: {slug}\n"
        "auto_generated: true\n"
        "brain_procedure_id: proc_1\n"
        "---\n"
        f"{body}",
        encoding="utf-8",
    )
    return d


def test_run_audit_scans_all_routed_hermes_profile_roots(tmp_path):
    claude = tmp_path / ".claude" / "skills"
    codex = tmp_path / ".codex" / "skills"
    profile_dirs = _profile_dirs(tmp_path)
    _write_auto_skill(profile_dirs["ellie"], "auto-infra")
    _write_auto_skill(profile_dirs["sage"], "auto-research")

    with (
        patch.object(skill_security_audit, "CLAUDE_SKILLS_DIR", claude),
        patch.object(skill_security_audit, "CODEX_SKILLS_DIR", codex),
        patch.object(skill_materializer, "HERMES_PROFILE_SKILLS_DIRS", profile_dirs),
    ):
        result = skill_security_audit.run_audit()

    assert result["totals"]["scanned"] == 2
    assert result["totals"]["no_attestation"] == 2
    assert str(profile_dirs["ellie"]) in result["per_root"]
    assert str(profile_dirs["sage"]) in result["per_root"]
    assert json.loads((profile_dirs["ellie"] / skill_materializer.USAGE_FILE).read_text())["auto-infra"][
        "content_sha256_origin"
    ] == "audit_backfill"


def test_clear_quarantine_checks_all_hermes_profile_roots(tmp_path):
    claude = tmp_path / ".claude" / "skills"
    codex = tmp_path / ".codex" / "skills"
    profile_dirs = _profile_dirs(tmp_path)
    root = profile_dirs["market"]
    root.mkdir(parents=True)
    (root / skill_materializer.USAGE_FILE).write_text(
        json.dumps({"auto-campaign": {"quarantined": True, "quarantine_reason": "test"}}),
        encoding="utf-8",
    )

    with (
        patch.object(skill_security_audit, "CLAUDE_SKILLS_DIR", claude),
        patch.object(skill_security_audit, "CODEX_SKILLS_DIR", codex),
        patch.object(skill_materializer, "HERMES_PROFILE_SKILLS_DIRS", profile_dirs),
    ):
        result = skill_security_audit.clear_quarantine("auto-campaign")

    assert result["cleared_in"] == [str(root)]
    usage = json.loads((root / skill_materializer.USAGE_FILE).read_text())
    assert usage["auto-campaign"]["quarantined"] is False
