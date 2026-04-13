"""Unit tests for brain_core.provenance — frontmatter cache + traversal."""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def isolated_provenance(tmp_path, monkeypatch):
    """Point provenance at a tmp_path canonical/distilled tree with seeded notes."""
    canonical = tmp_path / "knowledge" / "canonical" / "decisions"
    distilled = tmp_path / "knowledge" / "distilled" / "decisions"
    canonical.mkdir(parents=True)
    distilled.mkdir(parents=True)

    # Seed a small graph: A → B → C, plus an unrelated D
    (canonical / "a.md").write_text(
        '---\n{"id":"alpha","title":"Alpha","relations":'
        '[{"target":"beta","type":"depends_on"}]}\n---\nbody A'
    )
    (canonical / "b.md").write_text(
        '---\n{"id":"beta","title":"Beta","relations":' '[{"target":"gamma","type":"supports"}]}\n---\nbody B'
    )
    (canonical / "c.md").write_text('---\n{"id":"gamma","title":"Gamma","relations":[]}\n---\nbody C')
    (canonical / "d.md").write_text('---\n{"id":"delta","title":"Delta","relations":[]}\n---\nbody D')

    import provenance

    monkeypatch.setattr(provenance, "CANONICAL_DIR", canonical.parent)
    monkeypatch.setattr(provenance, "DISTILLED_DIR", distilled.parent)
    # Force cache rebuild
    monkeypatch.setattr(provenance, "_index_cache", None)
    monkeypatch.setattr(provenance, "_index_cache_ts", 0.0)
    yield provenance
    importlib.reload(provenance)


def test_unknown_id_returns_error(isolated_provenance):
    result = isolated_provenance.trace("does_not_exist")
    assert "error" in result
    assert "available_ids" in result


def test_trace_returns_tree_structure(isolated_provenance):
    tree = isolated_provenance.trace("alpha", max_depth=3)
    assert tree.get("id") == "alpha"
    # The tree is keyed differently per implementation — just ensure it's a dict and
    # contains the seeded id.
    assert isinstance(tree, dict)


def test_index_cache_hit(isolated_provenance):
    isolated_provenance._build_index()
    cache_after_first = isolated_provenance._index_cache
    isolated_provenance._build_index()
    cache_after_second = isolated_provenance._index_cache
    assert cache_after_first is cache_after_second, "cache should not rebuild on hit"


def test_reverse_relations_populated(isolated_provenance):
    index = isolated_provenance._build_index()
    # 'beta' is referenced by 'alpha' via depends_on
    beta = index.get("beta")
    assert beta is not None
    reverse = beta.get("_reverse_relations", [])
    assert any(src == "alpha" for src, _ in reverse), "alpha should appear in beta's reverse list"
