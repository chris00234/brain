"""Phase N1 — hot-path contradiction detection on POST /memory.

Verifies check_contradictions_for_memory fires on same-category near-duplicates
that share most wording but differ on a key content word, and that the Friston
predictive_error audit signal lands in action_audit.

Tests are pure-python (no live server) — we monkeypatch the VectorStore so
the nearest-neighbor stage returns a fixture pair and assert on the output.

Updated 2026-04-21: learn.py migrated to VectorStore; the mock now stands in
for get_vector_store() instead of chroma_api + ensure_collection directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


class _FakeStore:
    """Minimal VectorStore that returns a fixed query result and swallows writes."""

    name = "fake"

    def __init__(self, query_result):
        self._query_result = query_result
        self.deleted_ids: list[tuple[str, list[str]]] = []

    def heartbeat(self) -> bool:
        return True

    def list_collections(self):
        return ["semantic_memory", "semantic_contradictions"]

    def create_collection(self, name, metadata=None):
        return None

    def count(self, collection):
        return len(self._query_result)

    def query(self, collection, vector, k=10, *, filter=None, with_payload=True, with_vectors=False):
        return self._query_result

    def get(
        self,
        collection,
        ids=None,
        *,
        filter=None,
        limit=None,
        offset=0,
        with_payload=True,
        with_vectors=False,
        with_documents=True,
    ):
        return []

    def delete(self, collection, ids):
        self.deleted_ids.append((collection, list(ids)))

    def update_payload(self, collection, ids, patch):
        return None

    def upsert(self, collection, ids, vectors, payloads, documents=None):
        return None


def _make_hits(fixture):
    """Build VectorHit list from the pre-migration (ids, docs, dists, metas) shape."""
    from vector_store import VectorHit

    ids = fixture["ids"][0]
    docs = fixture["documents"][0]
    dists = fixture["distances"][0]
    metas = fixture["metadatas"][0]
    return [
        VectorHit(
            id=i,
            score=1.0 - float(dists[n]),  # ChromaStore's distance→similarity flip
            payload=dict(metas[n]) if metas[n] else {},
            document=docs[n],
        )
        for n, i in enumerate(ids)
    ]


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
    monkeypatch.setattr(learn, "_store_contradiction", lambda c: stored_contradictions.append(c))

    neighbor_fixture = {
        "ids": [["sem:other_irvine"]],
        "documents": [["Chris lives in Irvine"]],
        "distances": [[0.05]],
        "metadatas": [[{"category": "fact", "confidence": "0.6", "created_at": "2025-10-01T00:00:00Z"}]],
    }
    fake_store = _FakeStore(_make_hits(neighbor_fixture))
    monkeypatch.setattr(learn, "get_vector_store", lambda: fake_store)

    return learn, atoms_store, fake_store


def test_hot_path_detects_contradiction_and_fires_predictive_error(learn_with_stub):
    learn, atoms_store, _store = learn_with_stub
    contradictions = learn.check_contradictions_for_memory(
        mem_id="sem:new_la",
        content="Chris lives in LA",
        embedding=[0.1] * 1024,
        category="fact",
        confidence=0.9,
        created_at="2026-04-14T00:00:00Z",
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


def test_hot_path_skips_when_sym_diff_all_stopwords(monkeypatch, tmp_path):
    for mod in ("atoms_store", "config", "learn"):
        if mod in sys.modules:
            del sys.modules[mod]
    import atoms_store  # noqa: F401
    import learn

    fixture_same = {
        "ids": [["sem:dup"]],
        "documents": [["Chris likes coffee"]],
        "distances": [[0.05]],
        "metadatas": [
            [{"category": "preference", "confidence": "0.5", "created_at": "2025-10-01T00:00:00Z"}]
        ],
    }
    fake_store = _FakeStore(_make_hits(fixture_same))
    monkeypatch.setattr(learn, "get_vector_store", lambda: fake_store)
    monkeypatch.setattr(learn, "_store_contradiction", lambda _c: None)

    contradictions = learn.check_contradictions_for_memory(
        mem_id="sem:new",
        content="Chris likes coffee",
        embedding=[0.1] * 1024,
        category="preference",
        confidence=0.5,
        created_at="2026-04-14T00:00:00Z",
    )
    assert contradictions == [], "identical content should not fire a contradiction"


def test_hot_path_skips_when_different_category(monkeypatch, tmp_path):
    for mod in ("atoms_store", "config", "learn"):
        if mod in sys.modules:
            del sys.modules[mod]
    import atoms_store  # noqa: F401
    import learn

    fixture_crosscat = {
        "ids": [["sem:other"]],
        "documents": [["Chris lives in Irvine"]],
        "distances": [[0.05]],
        "metadatas": [
            [{"category": "preference", "confidence": "0.5", "created_at": "2025-10-01T00:00:00Z"}]
        ],
    }
    fake_store = _FakeStore(_make_hits(fixture_crosscat))
    monkeypatch.setattr(learn, "get_vector_store", lambda: fake_store)
    monkeypatch.setattr(learn, "_store_contradiction", lambda _c: None)

    contradictions = learn.check_contradictions_for_memory(
        mem_id="sem:new",
        content="Chris lives in LA",
        embedding=[0.1] * 1024,
        category="fact",
    )
    assert contradictions == [], "cross-category pairs must not contradict"
