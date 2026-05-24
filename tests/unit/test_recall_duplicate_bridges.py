"""Recall/store bridge tests — diagnose the failure mode where
``brain_recall(query)`` misses an existing memory but ``brain_store(content)``
then returns NOOP duplicate without telling the caller which existing memory
won, or how recall should have phrased the query to find it.

Phase 1 tests (RED before implementation):

1.  ``classify_operation`` surfaces the duplicate target id on NOOP (today it
    returns ``None`` in slot 2, throwing away the only handle the caller has
    to look the existing memory up).
2.  POST /memory NOOP response carries duplicate target metadata
    (id/collection/path/title/content_preview) — *not* the would-be id derived
    from the new content, which is what callers see today.
3.  When the caller passed ``recall_context_ids`` (the ids from their just-run
    recall) and the duplicate target's id is NOT in that list, the response
    sets ``duplicate_but_not_recalled=True`` and includes a
    ``suggested_bridge_query`` + ``recall_repair`` diagnostic so the next
    recall call can be repaired deterministically.
4.  Helper module ``recall_bridge`` builds the bridge query from exact aliases
    (code-ish tokens, paths, Hangul) found on the duplicate document.

Tests stay pure-python: vector store, atoms_store, and ingest_mirror are
monkeypatched so no live Qdrant/Neo4j/Ollama is required.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


# ── classify_operation: NOOP must surface duplicate target id ──────────


class _FakeQueryStore:
    """Minimal VectorStore stand-in for memory_operations.classify_operation.

    Returns a single near-exact hit so the classifier routes to NOOP.
    """

    def __init__(self, hit_id: str, hit_doc: str, hit_meta: dict, distance: float = 0.01):
        from vector_store import VectorHit

        self._hits = [
            VectorHit(
                id=hit_id,
                score=1.0 - distance,
                payload=hit_meta,
                document=hit_doc,
            )
        ]

    def query(self, collection, vector, k=5, *, filter=None, with_payload=True, with_vectors=False):
        return self._hits


def test_classify_operation_noop_returns_target_id(monkeypatch):
    """NOOP path must return the duplicate atom's chroma_id, not None.

    Today: ``classify_operation`` returns ``(\"NOOP\", None, diag)``. The id
    lives in ``diag[\"top_id\"]`` but every other op (UPDATE/DELETE) returns
    the id in slot 2 — only NOOP throws it away. That asymmetry is the root
    cause of the duplicate-without-id response.
    """
    import memory_operations

    store = _FakeQueryStore(
        hit_id="semantic_memory:abc123",
        hit_doc="Chris's wife's Korean name is 조혜진",
        hit_meta={"category": "fact", "confidence": 0.9},
        distance=0.01,
    )
    monkeypatch.setattr(memory_operations, "get_vector_store", lambda: store)

    op, target_id, diag = memory_operations.classify_operation(
        new_content="Chris's wife's Korean name is 조혜진",
        new_embedding=[0.1] * 1024,
        new_confidence=0.9,
        category="fact",
    )

    assert op == "NOOP", f"expected NOOP, got {op} (diag={diag})"
    assert target_id == "semantic_memory:abc123", (
        f"NOOP must surface the duplicate's chroma_id in slot 2, got {target_id!r}. "
        "Today this is None — see memory_operations.py:149."
    )


# ── recall_bridge helper: exact alias extraction ────────────────────────


def test_extract_exact_aliases_pulls_codey_tokens():
    """Code-ish/identifier tokens (uppercase, dotted, slashed, underscored)
    must survive intact so the bridge query and the exact-token boost in
    rerank both see them. Lowercase prose words don't qualify.
    """
    from recall_bridge import extract_exact_aliases

    text = (
        "Set CODEX_HOME to /Users/chrischo/.codex so the codex CLI picks up "
        "the claude1 claude2 claude3 claude4 profiles."
    )
    aliases = set(extract_exact_aliases(text))

    assert "CODEX_HOME" in aliases
    assert "/Users/chrischo/.codex" in aliases
    assert "claude1" in aliases
    assert "claude2" in aliases
    assert "claude3" in aliases
    assert "claude4" in aliases
    # Prose words and short tokens stay out.
    assert "the" not in aliases
    assert "set" not in aliases


def test_extract_exact_aliases_keeps_hangul_personal_names():
    """Korean names (Hangul) and mixed Hangul/Latin identity tokens must
    appear as bridge aliases so a recall like ``조대현`` reaches the right
    canonical identity atom.
    """
    from recall_bridge import extract_exact_aliases

    text = "Daehyun Cho Hangul name 조대현 — Chris's legal-style name on identity docs"
    aliases = set(extract_exact_aliases(text))

    assert "조대현" in aliases
    assert "Daehyun" in aliases
    assert "Cho" in aliases
    # Plain English connectives are excluded.
    assert "name" not in aliases


def test_build_suggested_bridge_query_uses_target_aliases():
    """The bridge query offered when a NOOP duplicate is detected must:
    - mention the new caller-supplied content first (so it stays
      intent-anchored), and
    - append a small set of exact aliases mined from the duplicate
      target's text/path/title so the next recall can hit on either lexical
      or vector match.
    """
    from recall_bridge import build_suggested_bridge_query

    bridge = build_suggested_bridge_query(
        new_content="codex home directory path",
        target_doc="CODEX_HOME=/Users/chrischo/.codex — codex CLI looks here for profiles",
        target_meta={
            "source_path": "/Users/chrischo/server/knowledge/codex/CODEX.md",
            "source_aliases": ["CODEX_HOME", "/Users/chrischo/.codex"],
            "title": "Codex CLI profiles",
        },
    )

    assert isinstance(bridge, str)
    assert "CODEX_HOME" in bridge
    # Path aliases survive so vector + lexical both fire.
    assert "/Users/chrischo/.codex" in bridge
    # The caller's original intent is preserved.
    assert "codex" in bridge.lower()


def test_compute_recall_repair_flags_missing_alias_tokens():
    """recall_repair surfaces the alias tokens that the caller's query did NOT
    include but the duplicate target carries — that gap is exactly why the
    initial recall missed.
    """
    from recall_bridge import compute_recall_repair

    repair = compute_recall_repair(
        query_content="what's the codex home directory",
        target_doc="CODEX_HOME=/Users/chrischo/.codex",
        target_meta={
            "source_aliases": ["CODEX_HOME", "/Users/chrischo/.codex"],
            "title": "Codex CLI profiles",
        },
    )

    assert isinstance(repair, dict)
    missing = set(repair.get("missing_tokens", []))
    assert (
        "CODEX_HOME" in missing
    ), f"CODEX_HOME absent from query → must appear in missing_tokens. got {repair}"
    assert "/Users/chrischo/.codex" in missing
    # ``exact_aliases`` lists every alias mined from the duplicate so the
    # caller (or an automated bridge) can pick which ones to use.
    aliases = set(repair.get("exact_aliases", []))
    assert {"CODEX_HOME", "/Users/chrischo/.codex"} <= aliases


# ── POST /memory NOOP response carries duplicate target metadata ────────


class _StoreFixture:
    """In-memory VectorStore stub that returns a fixed duplicate target hit
    when classify_operation queries semantic_memory and resolves the same id
    on ``get(ids=[...])`` so the route can enrich the NOOP response.
    """

    def __init__(self, dup_id: str, dup_doc: str, dup_meta: dict):
        from vector_store import VectorHit, VectorPoint

        self._dup_id = dup_id
        self._dup_doc = dup_doc
        self._dup_meta = dup_meta
        self._hit = VectorHit(id=dup_id, score=0.999, payload=dup_meta, document=dup_doc)
        self._point = VectorPoint(id=dup_id, payload=dup_meta, document=dup_doc, vector=None)

    def heartbeat(self) -> bool:
        return True

    def list_collections(self):
        return ["semantic_memory"]

    def create_collection(self, name, metadata=None):
        return None

    def count(self, collection):
        return 1

    def query(self, collection, vector, k=5, *, filter=None, with_payload=True, with_vectors=False):
        return [self._hit]

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
        if ids and self._dup_id in list(ids):
            return [self._point]
        return []

    def upsert(self, *a, **kw):
        return None

    def update_payload(self, *a, **kw):
        return None

    def delete(self, *a, **kw):
        return None


@pytest.fixture
def memory_route_stubbed(monkeypatch, tmp_path):
    """Wire the POST /memory route against a faked VectorStore + disable side
    effects (atoms mirror, contradiction probe, corroborate, embedding) so the
    test exercises only the NOOP response shape.
    """
    monkeypatch.setenv("BRAIN_DISABLE_ATOMS", "1")
    monkeypatch.setenv("BRAIN_CONTRADICT_ON_WRITE", "0")
    monkeypatch.setenv("BRAIN_CORROBORATE_ON_WRITE", "0")

    # Reload modules so route picks up patched store.
    for mod in ("vector_store", "memory_operations", "routes.memory"):
        if mod in sys.modules:
            del sys.modules[mod]

    dup_id = "semantic_memory:codex_home_dup"
    dup_doc = "CODEX_HOME=/Users/chrischo/.codex — codex CLI looks here for profiles"
    dup_meta = {
        "category": "fact",
        "confidence": 0.95,
        "source": "manual",
        "source_path": "/Users/chrischo/server/knowledge/codex/CODEX.md",
        "source_aliases": ["CODEX_HOME", "/Users/chrischo/.codex"],
        "title": "Codex CLI profiles",
    }

    import vector_store

    fake = _StoreFixture(dup_id, dup_doc, dup_meta)
    monkeypatch.setattr(vector_store, "get_vector_store", lambda: fake)

    import memory_operations

    monkeypatch.setattr(memory_operations, "get_vector_store", lambda: fake)

    from routes import memory as routes_memory

    monkeypatch.setattr(routes_memory, "get_vector_store", lambda: fake)

    # Embedding is a pure noop — the route just needs a non-empty vector.
    monkeypatch.setattr(routes_memory, "_get_embedding", lambda txt: [0.1] * 1024)

    return routes_memory, dup_id, dup_doc, dup_meta


def _post_memory(routes_memory, **req_kwargs):
    """Invoke the create_memory route function directly with a minimal stub
    Request — avoids spinning up the full FastAPI test client.
    """
    from fastapi import Request

    req_model = routes_memory.MemoryCreateRequest(**req_kwargs)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/memory",
        "headers": [],
        "query_string": b"",
    }
    request = Request(scope)
    return routes_memory.create_memory(request, req_model)


def test_create_memory_noop_response_includes_duplicate_target_metadata(memory_route_stubbed):
    """When the store collapses a write to NOOP, the response payload must
    expose the *existing* memory's id, source path, collection and content
    preview — not just the would-be id derived from the new content.
    """
    routes_memory, dup_id, dup_doc, dup_meta = memory_route_stubbed

    resp = _post_memory(
        routes_memory,
        content="CODEX_HOME points at /Users/chrischo/.codex for the codex CLI",
        category="fact",
        agent="claude",
        source="manual",
        confidence=0.9,
    )

    meta = resp.metadata
    assert meta.get("operation") == "NOOP"
    assert meta.get("duplicate_id") == dup_id, (
        "NOOP response must surface the existing memory's id (today the route "
        "returns the would-be id derived from new content — useless to callers)."
    )
    assert meta.get("duplicate_collection") == "semantic_memory"
    assert meta.get("duplicate_path") == dup_meta["source_path"]
    assert meta.get("duplicate_title") == dup_meta["title"]
    assert "CODEX_HOME" in meta.get("duplicate_content_preview", "")


def test_create_memory_noop_flags_duplicate_but_not_recalled(memory_route_stubbed):
    """If the caller passed ``recall_context_ids`` (their just-run recall hit
    ids) and the duplicate target is NOT in that list, NOOP response must
    flag ``duplicate_but_not_recalled=True`` and include a deterministic
    ``suggested_bridge_query`` so the caller can repair the next recall.
    """
    routes_memory, dup_id, _dup_doc, _dup_meta = memory_route_stubbed

    resp = _post_memory(
        routes_memory,
        content="codex CLI home directory",
        category="fact",
        agent="claude",
        source="manual",
        confidence=0.9,
        recall_context_ids=["semantic_memory:something_else_entirely"],
    )

    meta = resp.metadata
    assert meta.get("operation") == "NOOP"
    assert (
        meta.get("duplicate_but_not_recalled") is True
    ), f"duplicate {dup_id} was not in recall context — must be flagged. got {meta}"
    bridge = meta.get("suggested_bridge_query") or ""
    assert "CODEX_HOME" in bridge, (
        f"bridge query must include duplicate-target aliases so the next "
        f"recall reaches the missed memory. got: {bridge!r}"
    )
    repair = meta.get("recall_repair") or {}
    assert "CODEX_HOME" in (repair.get("exact_aliases") or [])


def test_create_memory_noop_no_recall_context_still_attaches_bridge(memory_route_stubbed):
    """When no recall context is passed, the duplicate_but_not_recalled flag
    stays absent (we can't know), but the bridge query + recall_repair fields
    are still attached so downstream debug surfaces have something to render.
    """
    routes_memory, _dup_id, _dup_doc, _dup_meta = memory_route_stubbed

    resp = _post_memory(
        routes_memory,
        content="codex CLI home directory",
        category="fact",
        agent="claude",
        source="manual",
        confidence=0.9,
    )

    meta = resp.metadata
    assert meta.get("operation") == "NOOP"
    assert meta.get("suggested_bridge_query"), "bridge query must be present even without recall context"
    assert "duplicate_but_not_recalled" not in meta, (
        "without recall_context_ids we can't decide → field must be omitted, not False, "
        f"got {meta.get('duplicate_but_not_recalled')!r}"
    )
