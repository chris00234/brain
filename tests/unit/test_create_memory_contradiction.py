"""Phase N1 — hot-path contradiction detection on POST /memory.

Verifies check_contradictions_for_memory fires on same-category near-duplicates
that share most wording but differ on a key content word, and that the Friston
predictive_error audit signal lands in action_audit.

Tests are pure-python (no live server) — we monkeypatch the Chroma query helper
so the nearest-neighbor stage returns a fixture pair and assert on the output.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


@pytest.fixture
def learn_with_stub(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAIN_ATOMS_ENABLED", "true")
    for mod in ("atoms_store", "config", "learn"):
        if mod in sys.modules:
            del sys.modules[mod]
    import atoms_store
    import learn

    fake_db = tmp_path / "brain.db"
    monkeypatch.setattr(atoms_store, "BRAIN_ATOMS_ENABLED", True)
    monkeypatch.setattr(atoms_store, "BRAIN_DB", fake_db)
    monkeypatch.setattr(atoms_store, "_initialized", False)
    atoms_store.init_schema(fake_db)

    stored_contradictions: list[dict] = []
    monkeypatch.setattr(
        learn, "_store_contradiction", lambda c: stored_contradictions.append(c)
    )
    monkeypatch.setattr(learn, "ensure_collection", lambda _name: None)
    monkeypatch.setattr(learn, "_get_collection_id", lambda _name: "sem_col_123")

    neighbor_response = {
        "ids": [["sem:other_irvine"]],
        "documents": [["Chris lives in Irvine"]],
        "distances": [[0.05]],
        "metadatas": [[{"category": "fact", "confidence": "0.6", "created_at": "2025-10-01T00:00:00Z"}]],
    }

    def _fake_chroma(method, path, body=None):
        if "/query" in path:
            return dict(neighbor_response)
        if "/delete" in path:
            return {}
        return {}

    monkeypatch.setattr(learn, "chroma_api", _fake_chroma)

    return learn, atoms_store


def test_hot_path_detects_contradiction_and_fires_predictive_error(learn_with_stub):
    learn, atoms_store = learn_with_stub
    contradictions = learn.check_contradictions_for_memory(
        mem_id="sem:new_la",
        content="Chris lives in LA",
        embedding=[0.1] * 1024,
        category="fact",
        confidence=0.9,
        created_at="2026-04-14T00:00:00Z",
        sem_col_id="sem_col_123",
    )
    assert len(contradictions) >= 1, "expected at least one contradiction"
    c = contradictions[0]
    assert c["new_id"] == "sem:new_la"
    assert c["old_id"] == "sem:other_irvine"
    assert c["review_state"] in {"pending", "auto_resolved"}

    with atoms_store._conn() as conn:
        row = conn.execute(
            "SELECT route, tool, query_text FROM action_audit WHERE tool='predictive_error'"
        ).fetchone()
    assert row is not None, "predictive_error action_audit row must exist"
    assert row["route"] == "/memory.contradiction"
    assert "LA" in (row["query_text"] or "")


def test_hot_path_skips_when_sym_diff_all_stopwords(learn_with_stub):
    learn, _ = learn_with_stub
    neighbor_response_same = {
        "ids": [["sem:dup"]],
        "documents": [["Chris likes coffee"]],
        "distances": [[0.05]],
        "metadatas": [[{"category": "preference", "confidence": "0.5", "created_at": "2025-10-01T00:00:00Z"}]],
    }

    def _fake_chroma(method, path, body=None):
        if "/query" in path:
            return dict(neighbor_response_same)
        return {}

    import importlib
    importlib.reload(learn)
    from unittest.mock import patch

    with patch.object(learn, "chroma_api", _fake_chroma), patch.object(
        learn, "ensure_collection", lambda _n: None
    ), patch.object(learn, "_get_collection_id", lambda _n: "sem_col_123"), patch.object(
        learn, "_store_contradiction", lambda _c: None
    ):
        contradictions = learn.check_contradictions_for_memory(
            mem_id="sem:new",
            content="Chris likes coffee",
            embedding=[0.1] * 1024,
            category="preference",
            confidence=0.5,
            created_at="2026-04-14T00:00:00Z",
            sem_col_id="sem_col_123",
        )
    assert contradictions == [], "identical content should not fire a contradiction"


def test_hot_path_skips_when_different_category(learn_with_stub):
    learn, _ = learn_with_stub

    neighbor_response_crosscat = {
        "ids": [["sem:other"]],
        "documents": [["Chris lives in Irvine"]],
        "distances": [[0.05]],
        "metadatas": [[{"category": "preference", "confidence": "0.5", "created_at": "2025-10-01T00:00:00Z"}]],
    }

    def _fake_chroma(method, path, body=None):
        if "/query" in path:
            return dict(neighbor_response_crosscat)
        return {}

    import importlib
    importlib.reload(learn)
    from unittest.mock import patch

    with patch.object(learn, "chroma_api", _fake_chroma), patch.object(
        learn, "ensure_collection", lambda _n: None
    ), patch.object(learn, "_get_collection_id", lambda _n: "sem_col_123"), patch.object(
        learn, "_store_contradiction", lambda _c: None
    ):
        contradictions = learn.check_contradictions_for_memory(
            mem_id="sem:new",
            content="Chris lives in LA",
            embedding=[0.1] * 1024,
            category="fact",
            sem_col_id="sem_col_123",
        )
    assert contradictions == [], "cross-category pairs must not contradict"
