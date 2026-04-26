from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core"))

import skill_materializer


def _procedure() -> dict:
    return {
        "id": "proc_codex_skill",
        "task_type": "codex skill sync",
        "title": "Sync generated skills to Codex",
        "steps": ["collect procedure", "render skill", "write runtimes"],
        "tools": ["Read", "Edit"],
        "success_count": 3,
        "source": "extraction",
    }


def test_materialize_writes_claude_codex_and_openclaw(tmp_path):
    claude = tmp_path / ".claude" / "skills"
    codex = tmp_path / ".codex" / "skills"
    openclaw = tmp_path / ".openclaw" / "skills"

    with (
        patch.object(skill_materializer, "CLAUDE_SKILLS_DIR", claude),
        patch.object(skill_materializer, "CODEX_SKILLS_DIR", codex),
        patch.object(skill_materializer, "OPENCLAW_SKILLS_DIR", openclaw),
        patch.object(skill_materializer, "_fetch_related_lessons", return_value=[]),
        patch.object(skill_materializer, "_sync_openclaw_registry", return_value={"ok": True}),
    ):
        result = skill_materializer.materialize(_procedure())

    assert result["materialized"] is True
    slug = "auto-codex-skill-sync"
    assert (claude / slug / "SKILL.md").exists()
    assert (codex / slug / "SKILL.md").exists()
    assert (openclaw / slug / "SKILL.md").exists()
    assert (openclaw / slug / "_meta.json").exists()
    assert any(".codex" in path for path in result["paths"])
    assert result["openclaw_sync"]["ok"] is True


def test_list_auto_skill_dirs_includes_codex(tmp_path):
    roots = [
        tmp_path / ".claude" / "skills",
        tmp_path / ".codex" / "skills",
        tmp_path / ".openclaw" / "skills",
    ]
    for root in roots:
        (root / "auto-example").mkdir(parents=True)

    with (
        patch.object(skill_materializer, "CLAUDE_SKILLS_DIR", roots[0]),
        patch.object(skill_materializer, "CODEX_SKILLS_DIR", roots[1]),
        patch.object(skill_materializer, "OPENCLAW_SKILLS_DIR", roots[2]),
    ):
        dirs = skill_materializer._list_auto_skill_dirs()

    assert {d.parent.parent.name for d in dirs} == {".claude", ".codex", ".openclaw"}
