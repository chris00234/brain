from __future__ import annotations

import json
import stat
from pathlib import Path
from unittest.mock import patch

import skill_sync


def _make_config(agent_skills: dict[str, list[str]]) -> dict:
    return {
        "meta": {},
        "agents": {"list": [{"id": aid, "skills": skills} for aid, skills in agent_skills.items()]},
        "skills": {"entries": {}},
    }


def _write_skill(root: Path, name: str, frontmatter_extra: str = "") -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\n" f"name: {name}\n" f"description: test {name}\n" f"{frontmatter_extra}" "---\n\n# Test\n",
        encoding="utf-8",
    )
    return d


def test_reconcile_registers_disk_skills_and_preserves_config_schema(tmp_path):
    skills_dir = tmp_path / "skills"
    config = tmp_path / "openclaw.json"
    telemetry = tmp_path / "skill_telemetry.json"
    config.write_text(json.dumps(_make_config({"jenna": []})), encoding="utf-8")
    _write_skill(skills_dir, "brain-learned-infra-ops")
    _write_skill(
        skills_dir, "auto-post-migration-audit", "auto_generated: true\nbrain_procedure_id: proc_1\n"
    )

    with (
        patch.object(skill_sync, "SKILLS_DIR", skills_dir),
        patch.object(skill_sync, "CONFIG_PATH", config),
        patch.object(skill_sync, "TELEMETRY_PATH", telemetry),
    ):
        stats = skill_sync.reconcile_registry()

    assert stats["registry_added"] == 2
    cfg = json.loads(config.read_text())
    assert cfg["skills"]["entries"]["brain-learned-infra-ops"] == {"enabled": True}
    assert cfg["skills"]["entries"]["auto-post-migration-audit"] == {"enabled": True}
    tel = json.loads(telemetry.read_text())
    assert tel["auto-post-migration-audit"]["auto_generated"] is True
    assert tel["auto-post-migration-audit"]["brain_procedure_id"] == "proc_1"


def test_attach_generated_skills_includes_brain_owned_auto_but_not_marketplace_auto(tmp_path):
    config = tmp_path / "openclaw.json"
    telemetry = tmp_path / "skill_telemetry.json"
    config.write_text(json.dumps(_make_config({"jenna": ["todoist"], "liz": []})), encoding="utf-8")
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

    with (
        patch.object(skill_sync, "CONFIG_PATH", config),
        patch.object(skill_sync, "TELEMETRY_PATH", telemetry),
    ):
        stats = skill_sync.attach_generated_skills()

    assert stats["attached"] == 4
    cfg = json.loads(config.read_text())
    for agent in cfg["agents"]["list"]:
        assert "brain-learned-infra-ops" in agent["skills"]
        assert "auto-post-migration-audit" in agent["skills"]
        assert "auto-updater" not in agent["skills"]
    assert cfg["agents"]["list"][0]["skills"][0] == "todoist"


def test_bump_agent_usage_counts_brain_owned_auto_skills(tmp_path):
    config = tmp_path / "openclaw.json"
    telemetry = tmp_path / "skill_telemetry.json"
    config.write_text(
        json.dumps(
            _make_config({"jenna": ["brain-learned-infra-ops", "auto-post-migration-audit", "auto-updater"]})
        ),
        encoding="utf-8",
    )
    telemetry.write_text(
        json.dumps(
            {
                "brain-learned-infra-ops": {
                    "path": "/x",
                    "description": "",
                    "auto_generated": False,
                    "use_count": 0,
                },
                "auto-post-migration-audit": {
                    "path": "/x",
                    "description": "",
                    "auto_generated": True,
                    "brain_procedure_id": "proc_1",
                    "use_count": 0,
                },
                "auto-updater": {"path": "/x", "description": "", "auto_generated": False, "use_count": 0},
            }
        ),
        encoding="utf-8",
    )

    with (
        patch.object(skill_sync, "CONFIG_PATH", config),
        patch.object(skill_sync, "TELEMETRY_PATH", telemetry),
    ):
        out = skill_sync.bump_agent_usage("jenna")

    assert out["ok"] is True
    assert out["bumped"] == 2
    tel = json.loads(telemetry.read_text())
    assert tel["brain-learned-infra-ops"]["use_count"] == 1
    assert tel["auto-post-migration-audit"]["use_count"] == 1
    assert tel["auto-updater"]["use_count"] == 0


def test_atomic_config_write_preserves_0600_permissions(tmp_path):
    skills_dir = tmp_path / "skills"
    config = tmp_path / "openclaw.json"
    telemetry = tmp_path / "skill_telemetry.json"
    config.write_text(json.dumps(_make_config({"jenna": []})), encoding="utf-8")
    config.chmod(0o600)
    _write_skill(skills_dir, "brain-learned-infra-ops")

    with (
        patch.object(skill_sync, "SKILLS_DIR", skills_dir),
        patch.object(skill_sync, "CONFIG_PATH", config),
        patch.object(skill_sync, "TELEMETRY_PATH", telemetry),
    ):
        skill_sync.reconcile_registry()

    assert stat.S_IMODE(config.stat().st_mode) == 0o600


def test_atomic_config_write_clamps_world_readable_config_to_0600(tmp_path):
    skills_dir = tmp_path / "skills"
    config = tmp_path / "openclaw.json"
    telemetry = tmp_path / "skill_telemetry.json"
    config.write_text(json.dumps(_make_config({"jenna": []})), encoding="utf-8")
    config.chmod(0o644)
    _write_skill(skills_dir, "brain-learned-infra-ops")

    with (
        patch.object(skill_sync, "SKILLS_DIR", skills_dir),
        patch.object(skill_sync, "CONFIG_PATH", config),
        patch.object(skill_sync, "TELEMETRY_PATH", telemetry),
    ):
        skill_sync.reconcile_registry()

    assert stat.S_IMODE(config.stat().st_mode) == 0o600
