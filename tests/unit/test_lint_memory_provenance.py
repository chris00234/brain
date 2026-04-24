from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_module():
    script = Path(__file__).resolve().parents[2] / "cli" / "lint_memory_provenance.py"
    spec = importlib.util.spec_from_file_location("lint_memory_provenance", script)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys_modules = __import__("sys").modules
    sys_modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


lint_memory_provenance = _load_module()


def _write_note(path: Path, metadata: dict, body: str = "Body") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---json\n" + json.dumps(metadata) + "\n---\n" + body + "\n")


def test_lint_detects_self_supersession_and_missing_relation(tmp_path):
    knowledge = tmp_path / "knowledge"
    _write_note(
        knowledge / "canonical" / "decisions" / "a.md",
        {
            "id": "a",
            "type": "canonical",
            "status": "active",
            "supersedes": ["a", "b"],
            "relations": [{"type": "mentions", "target": "b"}],
        },
    )
    _write_note(
        knowledge / "canonical" / "decisions" / "b.md",
        {"id": "b", "type": "canonical", "status": "active"},
    )

    report = lint_memory_provenance.lint(knowledge)
    codes = {issue["code"] for issue in report["issues"]}

    assert report["errors"] == 1
    assert "self_supersedes" in codes
    assert "missing_supersedes_relation" in codes


def test_lint_detects_missing_superseded_by_target(tmp_path):
    knowledge = tmp_path / "knowledge"
    _write_note(
        knowledge / "canonical" / "decisions" / "old.md",
        {
            "id": "old",
            "type": "canonical",
            "status": "superseded",
            "superseded_by": "missing_new",
        },
    )

    report = lint_memory_provenance.lint(knowledge)

    assert report["errors"] == 1
    assert report["issues"][0]["code"] == "missing_superseded_by_target"


def test_lint_detects_missing_distilled_source(tmp_path):
    knowledge = tmp_path / "knowledge"
    _write_note(
        knowledge / "canonical" / "decisions" / "canon.md",
        {
            "id": "canon",
            "type": "canonical",
            "status": "active",
            "sources": ["dist_missing"],
        },
    )

    report = lint_memory_provenance.lint(knowledge)

    assert report["errors"] == 0
    assert report["warnings"] == 1
    assert report["issues"][0]["code"] == "missing_distilled_source"


def test_lint_skips_canonical_support_markdown_without_frontmatter(tmp_path):
    knowledge = tmp_path / "knowledge"
    support_doc = knowledge / "canonical" / "live_state" / "active_goals.md"
    support_doc.parent.mkdir(parents=True, exist_ok=True)
    support_doc.write_text("# Active goals\n\nGenerated support snapshot.\n")

    report = lint_memory_provenance.lint(knowledge)

    assert report["errors"] == 0
    assert report["support_docs_skipped"] == 1


def test_repair_safe_removes_self_supersedes_and_adds_relation(tmp_path):
    knowledge = tmp_path / "knowledge"
    note = knowledge / "canonical" / "decisions" / "a.md"
    _write_note(
        note,
        {
            "id": "a",
            "type": "canonical",
            "status": "active",
            "supersedes": ["a", "b"],
            "relations": [],
        },
    )
    _write_note(
        knowledge / "canonical" / "decisions" / "b.md",
        {"id": "b", "type": "canonical", "status": "superseded"},
    )

    preview = lint_memory_provenance.repair_safe(knowledge, write=False)
    assert preview["change_count"] == 2
    before_meta = json.loads(note.read_text().split("---\n", 1)[0].removeprefix("---json\n"))
    assert before_meta["supersedes"] == ["a", "b"]

    applied = lint_memory_provenance.repair_safe(knowledge, write=True)
    assert applied["change_count"] == 2
    meta = json.loads(note.read_text().split("---\n", 1)[0].removeprefix("---json\n"))
    assert meta["supersedes"] == ["b"]
    assert {"type": "supersedes", "target": "b"} in meta["relations"]
    assert meta["provenance_repair"]["method"] == "lint_memory_provenance.safe_repair"


def test_repair_safe_renames_duplicate_distilled_ids_and_preserves_alias(tmp_path):
    knowledge = tmp_path / "knowledge"
    first = knowledge / "distilled" / "decisions" / "dup.md"
    second = knowledge / "distilled" / "incidents" / "dup.md"
    _write_note(first, {"id": "dist_dup", "type": "distilled", "status": "active"})
    _write_note(second, {"id": "dist_dup", "type": "distilled", "status": "active"})

    report = lint_memory_provenance.repair_safe(knowledge, write=True)

    assert any(change["code"] == "rename_duplicate_distilled_id" for change in report["changes"])
    first_meta = json.loads(first.read_text().split("---\n", 1)[0].removeprefix("---json\n"))
    second_meta = json.loads(second.read_text().split("---\n", 1)[0].removeprefix("---json\n"))
    assert first_meta["id"] != second_meta["id"]
    assert "dist_dup" in second_meta.get("source_aliases", [])
    assert "dist_dup" in second_meta.get("previous_ids", [])
    assert lint_memory_provenance.lint(knowledge)["errors"] == 0


def test_repair_safe_does_not_rename_duplicate_canonical_ids(tmp_path):
    knowledge = tmp_path / "knowledge"
    first = knowledge / "canonical" / "decisions" / "a.md"
    second = knowledge / "canonical" / "infra" / "a.md"
    _write_note(first, {"id": "same", "type": "canonical", "status": "active"})
    _write_note(second, {"id": "same", "type": "canonical", "status": "active"})

    report = lint_memory_provenance.repair_safe(knowledge, write=True)

    assert report["change_count"] == 0
    assert lint_memory_provenance.lint(knowledge)["errors"] == 2
