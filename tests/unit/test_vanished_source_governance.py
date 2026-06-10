"""Vanished-source provenance demotion (Contract 4).

A row whose absolute local source path no longer exists (deleted/moved/
retired document, e.g. a removed agent workspace) must rank BELOW living
documents for any current query — demoted, never dropped. The classifier is
purely provenance-derived: URLs, virtual ids, relative display paths, and
rows with any still-existing path candidate are never flagged. Fixture paths
are tmp_path placeholders, never real corpus paths.
"""

from __future__ import annotations

from recall_governance import source_authority as sa


def _clear_cache():
    sa._vanished_cache.clear()


# ── Classifier: positives ────────────────────────────────────────────────


def test_missing_absolute_path_is_vanished(tmp_path):
    _clear_cache()
    row = {"path": str(tmp_path / "removed-workspace" / "LEARNINGS.md")}
    assert sa.is_vanished_source_result(row) is True


def test_missing_metadata_source_path_is_vanished(tmp_path):
    _clear_cache()
    row = {"metadata": {"source_path": str(tmp_path / "gone.md")}}
    assert sa.is_vanished_source_result(row) is True


# ── Classifier: negative controls ────────────────────────────────────────


def test_existing_file_is_not_vanished(tmp_path):
    _clear_cache()
    alive = tmp_path / "alive.md"
    alive.write_text("current doc")
    assert sa.is_vanished_source_result({"path": str(alive)}) is False


def test_archived_but_existing_file_is_not_vanished(tmp_path):
    """Archived-on-disk docs still exist — they are NOT the vanished class
    (550 gated eval cases expect archived sources; only deletion demotes)."""
    _clear_cache()
    archived = tmp_path / "archived"
    archived.mkdir()
    doc = archived / "old-decision.md"
    doc.write_text("archived but present")
    assert sa.is_vanished_source_result({"path": str(doc)}) is False


def test_url_virtual_and_relative_sources_are_never_vanished():
    _clear_cache()
    for row in (
        {"path": "https://blog.example.com/post"},
        {"id": "erl_extraction"},
        {"id": "raw_events:raw_abc123"},
        {"path": "canonical/archived/decisions/old.md"},
        {"title": "route guarantee", "content": "no path at all"},
        {},
    ):
        assert sa.is_vanished_source_result(row) is False, row


def test_one_existing_candidate_rescues_row(tmp_path):
    """If ANY absolute path candidate still exists the row is not vanished
    (renamed mirrors often keep one valid pointer)."""
    _clear_cache()
    alive = tmp_path / "alive.md"
    alive.write_text("x")
    row = {"path": str(tmp_path / "gone.md"), "metadata": {"source_path": str(alive)}}
    assert sa.is_vanished_source_result(row) is False


# ── Governance ranking: vanished row sinks below living answer ───────────


def _governed(query, rows):
    from routes.recall import _apply_recall_governance_inplace

    fused = [dict(r) for r in rows]
    _apply_recall_governance_inplace(query, fused)
    fused.sort(key=lambda r: float(r.get("score") or 0.0), reverse=True)
    return fused


def test_vanished_workspace_row_sinks_below_living_knowledge_doc(tmp_path):
    _clear_cache()
    alive = tmp_path / "agents-communication.md"
    alive.write_text("How agents communicate: the message bus routes agent messages.")
    vanished = {
        "id": "v1",
        "title": "workspace learnings",
        "content": "Agents communicate through the workspace message relay and learnings file.",
        "path": str(tmp_path / "retired-workspace" / "LEARNINGS.md"),
        "collection": "obsidian",
        "score": 300.0,
    }
    living = {
        "id": "k1",
        "title": "agent communication design",
        "content": "Agents communicate through the message bus; each agent subscribes to its queue.",
        "path": str(alive),
        "collection": "knowledge",
        "score": 250.0,
    }
    ranked = _governed("how agents communicate", [vanished, living])
    assert [r["id"] for r in ranked] == ["k1", "v1"]
    v_row = next(r for r in ranked if r["id"] == "v1")
    assert "vanished_source_penalty" in (v_row.get("governance") or []), v_row
    # Demoted, never dropped.
    assert len(ranked) == 2


def test_vanished_penalty_applies_for_korean_query_too(tmp_path):
    """The class is query-language-independent: a KO paraphrase demotes the
    same vanished row."""
    _clear_cache()
    vanished = {
        "id": "v1",
        "title": "workspace learnings",
        "content": "에이전트들은 워크스페이스 릴레이로 통신한다.",
        "path": str(tmp_path / "retired" / "LEARNINGS.md"),
        "collection": "obsidian",
        "score": 300.0,
    }
    ranked = _governed("에이전트들은 어떻게 통신해?", [vanished])
    assert "vanished_source_penalty" in (ranked[0].get("governance") or [])


def test_living_doc_keeps_score_no_penalty(tmp_path):
    _clear_cache()
    alive = tmp_path / "doc.md"
    alive.write_text("content")
    living = {
        "id": "k1",
        "title": "doc",
        "content": "Agents communicate through the message bus.",
        "path": str(alive),
        "collection": "knowledge",
        "score": 100.0,
    }
    ranked = _governed("how agents communicate", [living])
    assert "vanished_source_penalty" not in (ranked[0].get("governance") or [])
