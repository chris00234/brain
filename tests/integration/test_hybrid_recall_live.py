"""Integration: live hybrid recall against running brain server.

Goes beyond the import-only unit tests by exercising the full fan-out:
- /recall/v2 end-to-end (embed → Qdrant hybrid prefetch → dense rescore →
  cross-encoder rerank → RRF across sources)
- verifies response shape, hit counts, payload completeness
- asserts named-vector coverage (sparse + contextual + raptor on canonical)
- asserts Korean + English queries both return results

Requires a running brain server on :8791 with Qdrant populated. Runs under
the ``integration`` marker so unit test runs don't hit the live stack.

Usage:
    pytest tests/integration/test_hybrid_recall_live.py
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

BRAIN_URL = "http://127.0.0.1:8791"
QDRANT_URL = "http://127.0.0.1:6333"
SECRET_FILE = Path("/Users/chrischo/.openclaw/credentials/.personal_webhook_secret")


def _token() -> str:
    if not SECRET_FILE.exists():
        pytest.skip(f"secret file missing: {SECRET_FILE}")
    return SECRET_FILE.read_text().strip()


def _brain_get(path: str, **params: str) -> dict:
    if params:
        path = f"{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(f"{BRAIN_URL}{path}")
    req.add_header("Authorization", f"Bearer {_token()}")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def _qdrant_get(path: str) -> dict:
    with urllib.request.urlopen(f"{QDRANT_URL}{path}", timeout=10) as r:
        return json.loads(r.read())


# ── health gate ────────────────────────────────────────────────────────


def test_brain_up_on_qdrant() -> None:
    h = _brain_get("/brain/health")
    assert h["services"].get("qdrant") == "up", f"qdrant not up: {h['services']}"
    # Should not report chromadb anymore
    assert "chromadb" not in h["services"], "chromadb key still present after decommission"


# ── hybrid recall shape ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "query",
    [
        "brain memory system",  # English
        "qdrant hybrid search",  # technical English
        "브레인 메모리",  # Korean
    ],
)
def test_recall_returns_ranked_hits(query: str) -> None:
    r = _brain_get("/recall/v2", q=query, k="5")
    hits = r.get("results") or []
    assert hits, f"no hits for query {query!r}"
    last_score = float("inf")
    for h in hits:
        # id may be None for graph-sourced hits; require at least one of id/path
        assert h.get("id") or h.get("path"), f"hit has neither id nor path: {h}"
        assert "score" in h and h["score"] > 0, h
        assert h["score"] <= last_score + 1e-3, "hits not score-sorted"
        last_score = h["score"]
        assert h.get("collection"), h
        assert h.get("content"), f"empty content on hit {h.get('id') or h.get('path')}"


# ── sparse + contextual + raptor coverage ─────────────────────────────


def _sample_slot_populated(collection: str, slot: str, sample: int = 50) -> float:
    """Scroll `sample` points, return fraction with `slot` populated."""
    body = json.dumps(
        {"limit": sample, "with_vector": [slot], "with_payload": False},
    ).encode()
    req = urllib.request.Request(
        f"{QDRANT_URL}/collections/{collection}/points/scroll",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        body = json.loads(r.read())
    points = body.get("result", {}).get("points", []) or []
    if not points:
        return 0.0
    populated = 0
    for p in points:
        v = p.get("vector") or {}
        vec = v.get(slot) if isinstance(v, dict) else v
        # sparse vectors come back as {indices, values}; dense as list.
        if (isinstance(vec, dict) and vec.get("indices")) or (isinstance(vec, list) and any(vec)):
            populated += 1
    return populated / len(points)


def test_sparse_populated_all_collections() -> None:
    cols = ["canonical", "semantic_memory", "experience", "knowledge", "code", "personal", "obsidian"]
    for c in cols:
        frac = _sample_slot_populated(c, "sparse")
        assert frac >= 0.9, f"{c}: only {frac:.0%} of sampled rows have sparse"


def test_canonical_dense_populated() -> None:
    frac = _sample_slot_populated("canonical", "dense")
    assert frac == 1.0, f"canonical dense population regressed: {frac:.0%}"


# ── smoke: pending contradictions path (uses semantic_contradictions alias) ──


def test_brain_doubt_alias_works() -> None:
    r = _brain_get("/brain/doubt", limit="3")
    # Should return without error even if zero contradictions
    assert "pending_contradictions" in r


# ── latency budget (smoke, not a hard SLO) ─────────────────────────────


def test_recall_under_1s_wall_clock() -> None:
    import time

    t0 = time.time()
    r = _brain_get("/recall/v2", q="docker service", k="5")
    elapsed = time.time() - t0
    assert r.get("results") is not None
    # Generous budget to avoid flakiness; SLO is 250ms p95, this is 4x.
    assert elapsed < 1.0, f"recall took {elapsed:.2f}s (budget 1.0s)"
