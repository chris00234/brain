from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_module():
    script = Path(__file__).resolve().parents[2] / "cli" / "backfill_distilled_provenance.py"
    spec = importlib.util.spec_from_file_location("backfill_distilled_provenance", script)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_note(path: Path, metadata: dict, body: str = "## Statement\n\nBody text with enough content."):
    from pipeline.common import write_markdown_frontmatter

    write_markdown_frontmatter(path, metadata, body)


def test_dry_run_reports_exact_distilled_source_without_writing(tmp_path):
    module = _load_module()
    knowledge = tmp_path / "knowledge"
    canonical = knowledge / "canonical" / "archived" / "chris" / "canon.md"
    distilled = knowledge / "distilled" / "chris" / "dist_alpha.md"
    _write_note(
        canonical,
        {"id": "canon_alpha", "title": "Canon", "sources": ["dist_alpha"]},
    )
    _write_note(
        distilled,
        {"id": "dist_alpha", "title": "Distilled", "source_aliases": ["existing_alias"]},
    )

    changes = module.backfill(knowledge, write=False)

    assert len(changes) == 1
    from pipeline.common import parse_note

    metadata, _body = parse_note(distilled)
    assert metadata["source_aliases"] == ["existing_alias"]
    assert "supersedes" not in metadata


def test_write_merges_provenance_without_dropping_existing_values(tmp_path):
    module = _load_module()
    knowledge = tmp_path / "knowledge"
    canonical = knowledge / "canonical" / "archived" / "chris" / "canon.md"
    distilled = knowledge / "distilled" / "chris" / "dist_alpha.md"
    _write_note(
        canonical,
        {"id": "canon_alpha", "title": "Canon", "sources": ["dist_alpha"]},
    )
    _write_note(
        distilled,
        {
            "id": "dist_alpha",
            "title": "Distilled",
            "supersedes": ["older_canon"],
            "source_aliases": ["existing_alias"],
            "relations": [{"type": "derived_from", "target": "raw_1"}],
        },
    )

    changes = module.backfill(knowledge, write=True)

    assert len(changes) == 1
    from pipeline.common import parse_note

    metadata, body = parse_note(distilled)
    assert body.startswith("## Statement")
    assert metadata["supersedes"] == ["older_canon", "canon_alpha"]
    assert "existing_alias" in metadata["source_aliases"]
    assert "canon_alpha" in metadata["source_aliases"]
    assert "canonical/archived/chris/canon.md" in metadata["source_aliases"]
    assert {"type": "derived_from", "target": "raw_1"} in metadata["relations"]
    assert {"type": "supersedes", "target": "canon_alpha"} in metadata["relations"]
    assert metadata["provenance_backfill"]["method"] == "canonical_sources_distilled_id"


def test_ignores_non_exact_and_non_distilled_sources(tmp_path):
    module = _load_module()
    knowledge = tmp_path / "knowledge"
    canonical = knowledge / "canonical" / "canon.md"
    distilled = knowledge / "distilled" / "dist_alpha.md"
    _write_note(
        canonical,
        {"id": "canon_alpha", "title": "Canon", "sources": ["raw_1", "dist_missing"]},
    )
    _write_note(distilled, {"id": "dist_alpha", "title": "Distilled"})

    assert module.backfill(knowledge, write=True) == []

    from pipeline.common import parse_note

    metadata, _body = parse_note(distilled)
    assert "supersedes" not in metadata
