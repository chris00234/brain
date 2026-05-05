from __future__ import annotations

# ruff: noqa: E402,I001

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core"))

import indexer
import search_unified
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


def test_prepare_index_document_adds_stable_document_mapping(tmp_path):
    source = tmp_path / "docs" / "alpha.md"
    source.parent.mkdir()
    source.write_text("# Alpha\n\nThis is a sufficiently long document body for indexing.")

    prepared = indexer.prepare_index_document(
        {
            "source": str(source),
            "content": source.read_text(),
            "type": "obsidian-note",
            "title": "Alpha Document",
            "section": "full",
        }
    )

    assert prepared is not None
    _doc_id, _content, meta, _embed_text = prepared
    assert meta["document_id"].startswith("doc:")
    assert meta["source_document_id"] == meta["document_id"]
    assert meta["source_path"] == str(source)
    assert meta["source_name"] == "alpha.md"
    assert "Alpha Document" in meta["source_aliases"]


def test_normalize_rag_result_preserves_document_mapping():
    result = search_unified.normalize_rag_result(
        {
            "id": "chunk-1",
            "collection": "knowledge",
            "source": "/tmp/source.md",
            "content": "This content is long enough to look like a real indexed chunk.",
            "type": "manual-note",
            "section": "Intro",
            "metadata": {"document_id": "doc:source:abc", "source_path": "/tmp/source.md"},
        }
    )

    assert result["metadata"]["document_id"] == "doc:source:abc"
    assert result["metadata"]["source_document_id"].startswith("doc:")
    assert result["metadata"]["source_path"] == "/tmp/source.md"
    assert result["path"] == "/tmp/source.md"


def test_prepare_index_document_adds_policy_tags(monkeypatch):
    monkeypatch.setenv("BRAIN_SEMANTIC_CHUNKING", "1")
    prepared = indexer.prepare_index_document(
        {
            "source": "/tmp/source.md",
            "content": "This is a long natural-language source. " * 20,
            "type": "obsidian-note",
            "domain": "chris",
            "tags": ["Personal Knowledge"],
        }
    )

    assert prepared is not None
    _doc_id, _content, meta, _embed_text = prepared
    assert meta["chunk_strategy"] == "semantic"
    assert meta["semantic_chunk_candidate"] is True
    assert "personal-knowledge" in meta["tags"]
    assert "domain:chris" in meta["tags"]
    assert meta["context_tags"] == meta["tags"]
