from __future__ import annotations

# ruff: noqa: E402,I001

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core"))

import indexer
from pipeline.common import write_markdown_frontmatter


def test_canonical_doc_provenance_fields_expands_aliases():
    source = "/Users/chrischo/server/knowledge/distilled/chris/dist-alpha.md"
    fields = indexer._canonical_doc_provenance_fields(
        {
            "id": "dist_alpha",
            "title": "Distilled Alpha",
            "domain": "chris",
            "sources": ["raw_1"],
            "supersedes": ["canon_alpha"],
            "relations": [{"type": "source", "target": "canonical/chris/canon-alpha.md"}],
            "source_aliases": ["manual_alias"],
        },
        source,
    )

    assert fields["note_id"] == "dist_alpha"
    assert fields["note_title"] == "Distilled Alpha"
    assert fields["sources"] == ["raw_1"]
    assert fields["supersedes"] == ["canon_alpha"]
    assert "manual_alias" in fields["source_aliases"]
    assert "dist_alpha" in fields["source_aliases"]
    assert "dist-alpha" in fields["source_aliases"]
    assert "canon_alpha" in fields["source_aliases"]
    assert "canonical/chris/canon-alpha.md" in fields["source_aliases"]


def test_collect_canonical_includes_frontmatter_provenance(tmp_path, monkeypatch):
    brain_home = tmp_path / "server"
    note = brain_home / "knowledge" / "distilled" / "chris" / "dist_alpha.md"
    write_markdown_frontmatter(
        note,
        {
            "id": "dist_alpha",
            "title": "Distilled Alpha",
            "sources": ["raw_1"],
            "supersedes": ["canon_alpha"],
            "relations": [{"type": "supersedes", "target": "canon_alpha"}],
        },
        "## Statement\n\nThis distilled note has enough searchable content to be indexed.",
    )
    monkeypatch.setattr(indexer, "BRAIN_HOME", brain_home)

    docs = indexer.collect_canonical()

    assert docs
    assert all(doc["note_id"] == "dist_alpha" for doc in docs)
    assert all(doc["sources"] == ["raw_1"] for doc in docs)
    assert all(doc["supersedes"] == ["canon_alpha"] for doc in docs)
    assert all("canon_alpha" in doc["source_aliases"] for doc in docs)
