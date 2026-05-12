"""Unit tests for brain_core.eval_holdout_promote (Phase C1)."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


@pytest.fixture
def isolated_promote(tmp_path, monkeypatch):
    """Wire eval_holdout_promote against tmp_path eval set + a stubbed embedder.

    eval_proposals migrated to db.open_autonomy_db on 2026-05-12; the canonical
    AUTONOMY_DB now lives on the shared db module.
    """
    for mod in ("eval_holdout_promote", "eval_proposals", "config", "db"):
        if mod in sys.modules:
            del sys.modules[mod]
    import db as _db
    import eval_holdout_promote
    import eval_proposals

    fake_db = tmp_path / "autonomy.db"
    monkeypatch.setattr(_db, "AUTONOMY_DB", fake_db)
    _db._schema_cache.clear()
    monkeypatch.setattr(eval_holdout_promote, "list_candidates", eval_proposals.list_candidates)
    monkeypatch.setattr(eval_holdout_promote, "mark_status", eval_proposals.mark_status)

    eval_set = tmp_path / "eval_set.json"
    eval_set.write_text(json.dumps([{"query": "existing one"}, {"query": "another one"}]))
    pending = tmp_path / "eval_holdout_pending.json"
    monkeypatch.setattr(eval_holdout_promote, "EVAL_SET_PATH", eval_set)
    monkeypatch.setattr(eval_holdout_promote, "PENDING_PATH", pending)

    yield eval_holdout_promote, eval_proposals, pending
    importlib.reload(eval_holdout_promote)
    importlib.reload(eval_proposals)


def _stub_embedder(promote_mod, monkeypatch, query_to_emb):
    """Return a fake _embed that yields predetermined vectors per query."""

    def fake_embed(text):
        return query_to_emb.get(text)

    monkeypatch.setattr(promote_mod, "_embed", fake_embed)


def test_run_no_candidates_returns_zero(isolated_promote):
    promote, _, _ = isolated_promote
    result = promote.run()
    assert result["checked"] == 0
    assert result["promoted"] == 0


def test_run_promotes_novel_candidates(isolated_promote, monkeypatch):
    promote, proposals, pending = isolated_promote

    # Existing eval queries embed to (1,0,0)
    # Novel candidate embeds to (0,1,0) → similarity 0 → novelty 1.0 → promote
    embeddings = {
        "existing one": [1.0, 0.0, 0.0],
        "another one": [1.0, 0.0, 0.0],
        "totally new query about cooking": [0.0, 1.0, 0.0],
    }
    _stub_embedder(promote, monkeypatch, embeddings)

    proposals.insert_proposal(
        query="totally new query about cooking",
        expected="recipe",
    )

    result = promote.run()
    assert result["checked"] == 1
    assert result["promoted"] == 1
    assert result["rejected"] == 0

    # Pending file should now contain the candidate
    payload = json.loads(pending.read_text())
    assert len(payload) == 1
    assert payload[0]["query"] == "totally new query about cooking"
    assert payload[0]["novelty"] >= 0.7


def test_run_rejects_near_duplicate_candidates(isolated_promote, monkeypatch):
    promote, proposals, _ = isolated_promote

    # Candidate embeds to ~(0.99, 0.01, 0) → similarity ~0.99 → novelty 0.01 → reject
    embeddings = {
        "existing one": [1.0, 0.0, 0.0],
        "another one": [1.0, 0.0, 0.0],
        "near dup query": [0.99, 0.01, 0.0],
    }
    _stub_embedder(promote, monkeypatch, embeddings)

    proposals.insert_proposal(query="near dup query", expected="x")

    result = promote.run()
    assert result["checked"] == 1
    assert result["promoted"] == 0
    assert result["rejected"] == 1

    # Verify the candidate's status moved to 'rejected'
    rejected = proposals.list_candidates(status="rejected")
    assert len(rejected) == 1
    assert rejected[0]["query"] == "near dup query"


def test_top_n_cap(isolated_promote, monkeypatch):
    promote, proposals, pending = isolated_promote

    # 8 candidates, all novel — TOP_N is 5 by default
    embeddings = {"existing one": [1.0, 0.0, 0.0], "another one": [1.0, 0.0, 0.0]}
    for i in range(8):
        q = f"novel candidate {i}"
        embeddings[q] = [0.0, 1.0, float(i) / 10]
        proposals.insert_proposal(query=q, expected=f"e{i}")
    _stub_embedder(promote, monkeypatch, embeddings)

    result = promote.run()
    assert result["checked"] == 8
    assert result["promoted"] == promote.TOP_N
    payload = json.loads(pending.read_text())
    assert len(payload) == promote.TOP_N
