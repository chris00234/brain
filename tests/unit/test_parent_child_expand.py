from __future__ import annotations

import importlib


def _reload_parent_child_expand(monkeypatch, enabled: bool):
    if enabled:
        monkeypatch.setenv("BRAIN_PARENT_CHILD_EXPAND", "1")
    else:
        monkeypatch.delenv("BRAIN_PARENT_CHILD_EXPAND", raising=False)
    import brain_core.parent_child_expand as parent_child_expand

    return importlib.reload(parent_child_expand)


def test_expand_to_parents_is_noop_when_disabled(monkeypatch):
    parent_child_expand = _reload_parent_child_expand(monkeypatch, enabled=False)
    rows = [{"content": "child", "metadata": {"parent_id": "parent-1"}}]

    assert parent_child_expand.expand_to_parents(rows) is rows
    assert rows[0]["content"] == "child"
    assert rows[0]["metadata"] == {"parent_id": "parent-1"}


def test_expand_to_parents_batches_unique_parent_fetches(monkeypatch):
    parent_child_expand = _reload_parent_child_expand(monkeypatch, enabled=True)
    parent_child_expand._CACHE.clear()
    fetched_parent_ids: list[list[str]] = []

    def fake_fetch(parent_ids: list[str]) -> dict[str, str]:
        fetched_parent_ids.append(parent_ids)
        return {"parent-1": "expanded parent content"}

    monkeypatch.setattr(parent_child_expand, "_fetch_parents_from_chroma", fake_fetch)

    rows = [
        {"content": "child one", "metadata": {"parent_id": "parent-1", "chunk_id": "child-1"}},
        {"content": "child two", "metadata": {"parent_id": "parent-1", "chunk_id": "child-2"}},
        {"content": "standalone", "metadata": {"chunk_id": "standalone"}},
    ]

    out = parent_child_expand.expand_to_parents(rows)

    assert out is rows
    assert fetched_parent_ids == [["parent-1"]]
    assert rows[0]["content"] == "expanded parent content"
    assert rows[1]["content"] == "expanded parent content"
    assert rows[2]["content"] == "standalone"
    assert rows[0]["metadata"]["parent_expanded"] is True
    assert rows[0]["metadata"]["child_content"] == "child one"
    assert rows[1]["metadata"]["parent_expanded"] is True
    assert rows[1]["metadata"]["child_content"] == "child two"


def test_expand_to_parents_uses_cache_after_first_fetch(monkeypatch):
    parent_child_expand = _reload_parent_child_expand(monkeypatch, enabled=True)
    parent_child_expand._CACHE.clear()
    fetch_count = 0

    def fake_fetch(parent_ids: list[str]) -> dict[str, str]:
        nonlocal fetch_count
        fetch_count += 1
        return {"parent-1": "expanded parent content"}

    monkeypatch.setattr(parent_child_expand, "_fetch_parents_from_chroma", fake_fetch)

    parent_child_expand.expand_to_parents([{"content": "child one", "metadata": {"parent_id": "parent-1"}}])
    second = [{"content": "child two", "metadata": {"parent_id": "parent-1"}}]
    parent_child_expand.expand_to_parents(second)

    assert fetch_count == 1
    assert second[0]["content"] == "expanded parent content"
