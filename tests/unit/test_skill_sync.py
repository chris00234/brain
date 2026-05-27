from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import skill_sync


def _profile_dirs(tmp_path: Path) -> dict[str, Path]:
    return {
        profile: tmp_path / ".hermes" / "profiles" / profile / "skills"
        for profile in skill_sync.HERMES_PROFILE_NAMES
    }


def _write_skill(root: Path, name: str, frontmatter_extra: str = "") -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\n" f"name: {name}\n" f"description: test {name}\n" f"{frontmatter_extra}" "---\n\n# Test\n",
        encoding="utf-8",
    )
    return d


def test_reconcile_registers_disk_skills_across_all_hermes_profiles(tmp_path):
    profile_dirs = _profile_dirs(tmp_path)
    telemetry = tmp_path / "skill_telemetry.json"
    _write_skill(profile_dirs["ellie"], "brain-learned-infra-ops")
    _write_skill(
        profile_dirs["sage"],
        "auto-paper-research",
        'auto_generated: true\nbrain_procedure_id: proc_1\nbrain_domain: paper\ntarget_profiles: ["sage"]\nmaterialized_for: ["claude", "codex", "hermes:sage"]\n',
    )

    with (
        patch.object(skill_sync, "HERMES_PROFILE_SKILLS_DIRS", profile_dirs),
        patch.object(skill_sync, "TELEMETRY_PATH", telemetry),
    ):
        stats = skill_sync.reconcile_registry()

    assert stats["registry_added"] == 0
    assert stats["telemetry_added"] == 2
    assert stats["total_on_disk"] == 2
    tel = json.loads(telemetry.read_text())
    assert tel["brain-learned-infra-ops"]["profiles"] == ["ellie"]
    assert tel["auto-paper-research"]["auto_generated"] is True
    assert tel["auto-paper-research"]["brain_procedure_id"] == "proc_1"
    assert tel["auto-paper-research"]["brain_domain"] == "paper"
    assert tel["auto-paper-research"]["target_profiles"] == ["sage"]
    assert tel["auto-paper-research"]["materialized_for"] == ["claude", "codex", "hermes:sage"]


def test_reconcile_merges_same_skill_name_across_profiles(tmp_path):
    profile_dirs = _profile_dirs(tmp_path)
    telemetry = tmp_path / "skill_telemetry.json"
    _write_skill(profile_dirs["ellie"], "auto-shared", "auto_generated: true\nbrain_procedure_id: proc_2\n")
    _write_skill(profile_dirs["liz"], "auto-shared", "auto_generated: true\nbrain_procedure_id: proc_2\n")

    with (
        patch.object(skill_sync, "HERMES_PROFILE_SKILLS_DIRS", profile_dirs),
        patch.object(skill_sync, "TELEMETRY_PATH", telemetry),
    ):
        skill_sync.reconcile_registry()

    tel = json.loads(telemetry.read_text())
    entry = tel["auto-shared"]
    assert entry["profiles"] == ["ellie", "liz"]
    assert set(entry["paths"]) == {"ellie", "liz"}
    assert entry["path"] == str(profile_dirs["liz"] / "auto-shared")


def test_reconcile_dry_run_does_not_write_telemetry(tmp_path):
    profile_dirs = _profile_dirs(tmp_path)
    telemetry = tmp_path / "skill_telemetry.json"
    _write_skill(profile_dirs["market"], "auto-seo-campaign", "auto_generated: true\nbrain_procedure_id: proc_3\n")

    with (
        patch.object(skill_sync, "HERMES_PROFILE_SKILLS_DIRS", profile_dirs),
        patch.object(skill_sync, "TELEMETRY_PATH", telemetry),
    ):
        stats = skill_sync.reconcile_registry(dry_run=True)

    assert stats["dry_run"] is True
    assert stats["telemetry_added"] == 1
    assert not telemetry.exists()


def test_attach_generated_skills_counts_brain_owned_auto_but_not_marketplace_auto(tmp_path):
    telemetry = tmp_path / "skill_telemetry.json"
    telemetry.write_text(
        json.dumps(
            {
                "brain-learned-infra-ops": {"path": "/x", "description": "", "auto_generated": False},
                "auto-post-migration-audit": {
                    "path": "/x",
                    "description": "",
                    "auto_generated": True,
                    "brain_procedure_id": "proc_1",
                },
                "auto-updater": {"path": "/x", "description": "", "auto_generated": False},
            }
        ),
        encoding="utf-8",
    )

    with patch.object(skill_sync, "TELEMETRY_PATH", telemetry):
        stats = skill_sync.attach_generated_skills()

    assert stats["attached"] == 0
    assert stats["generated_skills_registered"] == 2
    assert stats["agents_touched"] == []


def test_bump_agent_usage_counts_only_matching_profile_generated_skills(tmp_path):
    profile_dirs = _profile_dirs(tmp_path)
    telemetry = tmp_path / "skill_telemetry.json"
    telemetry.write_text(
        json.dumps(
            {
                "brain-learned-infra-ops": {
                    "paths": {"ellie": str(profile_dirs["ellie"] / "brain-learned-infra-ops")},
                    "description": "",
                    "auto_generated": False,
                    "use_count": 0,
                },
                "auto-post-migration-audit": {
                    "paths": {"liz": str(profile_dirs["liz"] / "auto-post-migration-audit")},
                    "description": "",
                    "auto_generated": True,
                    "brain_procedure_id": "proc_1",
                    "use_count": 0,
                },
                "auto-updater": {
                    "paths": {"ellie": str(profile_dirs["ellie"] / "auto-updater")},
                    "description": "",
                    "auto_generated": False,
                    "use_count": 0,
                },
            }
        ),
        encoding="utf-8",
    )

    with (
        patch.object(skill_sync, "HERMES_PROFILE_SKILLS_DIRS", profile_dirs),
        patch.object(skill_sync, "TELEMETRY_PATH", telemetry),
    ):
        out = skill_sync.bump_agent_usage("ellie")

    assert out["ok"] is True
    assert out["bumped"] == 1
    tel = json.loads(telemetry.read_text())
    assert tel["brain-learned-infra-ops"]["use_count"] == 1
    assert tel["auto-post-migration-audit"]["use_count"] == 0
    assert tel["auto-updater"]["use_count"] == 0
