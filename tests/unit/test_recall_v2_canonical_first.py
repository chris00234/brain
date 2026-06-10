"""Behavioral tests for /recall/v2 canonical_first=true (llm-wiki truth-layer mode).

canonical_first is a truth-layer-only contract: sources restrict to
["canonical"], and non-canonical synthetic injections (raw_events FTS factoid
rescue) and the CRAG retry must honor it end-to-end. Before these tests, the
flag was only covered by cache-key parametrization — the behavioral branches
had no regression coverage (CRAG retry silently dropped the flag, and the
factoid rescue injected raw_events_fts rows into canonical-only responses).
"""

from __future__ import annotations

from starlette.requests import Request


def _req() -> Request:
    return Request(
        {"type": "http", "method": "GET", "path": "/recall/v2", "headers": [], "query_string": b""}
    )


def _canonical_row(rid: str, content: str, score: float = 50.0) -> dict:
    return {
        "id": rid,
        "title": rid,
        "content": content,
        "collection": "canonical",
        "metadata": {"category": "fact", "review_state": "accepted"},
        "score": score,
    }


def _install_search(monkeypatch, sources_seen: list) -> None:
    import search_unified

    def fake_search_all(query, limit, *, sources=None, **kw):
        sources_seen.append(list(sources) if sources else None)
        row = _canonical_row(
            "canon1",
            "Deployment convention: every service ships as a docker container "
            "with Uptime Kuma registration and a Glance dashboard entry.",
        )
        return {"results": [row], "total_candidates": 1}

    monkeypatch.setattr(search_unified, "search_all", fake_search_all)


def _handler():
    from routes import recall as R

    R._recall_cache.clear()
    return R, getattr(R.recall_v2, "__wrapped__", R.recall_v2)


def test_canonical_first_restricts_every_search_to_canonical_sources(monkeypatch):
    sources_seen: list = []
    _install_search(monkeypatch, sources_seen)
    _, fn = _handler()
    fn(_req(), "docker deployment conventions homelab", n=5, collection=None, canonical_first=True)
    assert sources_seen, "search_all never called"
    assert all(s == ["canonical"] for s in sources_seen), sources_seen


def test_default_mode_keeps_broad_sources(monkeypatch):
    """Negative control: without canonical_first the broad source set is used."""
    sources_seen: list = []
    _install_search(monkeypatch, sources_seen)
    _, fn = _handler()
    fn(_req(), "docker deployment conventions homelab", n=5, collection=None, canonical_first=False)
    assert sources_seen, "search_all never called"
    assert all(s == ["rag", "canonical", "obsidian"] for s in sources_seen), sources_seen


_FACTOID_PROBE = "What should I remember about Chris OMSCS Fall 2026?"
_FACTOID_ANSWER = "OMSCS: Chris is enrolling in the OMSCS program starting Fall 2026."


def _install_factoid_fts(monkeypatch, calls: list) -> None:
    import raw_events_fts

    def fake_fts_search(query, limit=8, **kw):
        calls.append(query)
        return [{"id": "raw_factoid1", "title": "", "content": _FACTOID_ANSWER, "raw_source_type": "note"}]

    monkeypatch.setattr(raw_events_fts, "search", fake_fts_search)


def test_personal_factoid_fts_injection_fires_in_default_mode(monkeypatch):
    """Positive control: the raw_events FTS factoid rescue still serves the
    durable answer for a pure personal-fact probe in broad mode."""
    sources_seen: list = []
    fts_calls: list = []
    _install_search(monkeypatch, sources_seen)
    _install_factoid_fts(monkeypatch, fts_calls)
    _, fn = _handler()
    resp = fn(_req(), _FACTOID_PROBE, n=5, collection=None, canonical_first=False)
    collections = {str(r.get("collection")) for r in resp.results}
    assert fts_calls, "factoid FTS rescue did not run in broad mode"
    assert "raw_events_fts" in collections, resp.results


def test_personal_factoid_fts_injection_suppressed_in_canonical_first(monkeypatch):
    """canonical_first must not leak raw_events FTS rows into a canonical-only
    response — the factoid rescue is skipped entirely."""
    sources_seen: list = []
    fts_calls: list = []
    _install_search(monkeypatch, sources_seen)
    _install_factoid_fts(monkeypatch, fts_calls)
    _, fn = _handler()
    resp = fn(_req(), _FACTOID_PROBE, n=5, collection=None, canonical_first=True)
    collections = {str(r.get("collection")) for r in resp.results}
    assert not fts_calls, "factoid FTS rescue ran in canonical_first mode"
    assert "raw_events_fts" not in collections, resp.results


def test_crag_retry_preserves_canonical_first(monkeypatch):
    """The CRAG iterative retry recurses into recall_v2; it must forward
    canonical_first so the retry stays truth-layer-only."""
    sources_seen: list = []
    _install_search(monkeypatch, sources_seen)
    R, fn = _handler()

    captured: dict = {}

    def fake_run_crag_retry(q, n, fused, retry_fn):
        captured["retry_fn"] = retry_fn
        return fused, 0, {}, None

    monkeypatch.setattr(R, "_decide_use_crag", lambda q, iterative: (True, None))
    monkeypatch.setattr(R, "_run_crag_retry", fake_run_crag_retry)

    fn(
        _req(),
        "docker deployment conventions homelab",
        n=5,
        collection=None,
        iterative=True,
        canonical_first=True,
    )
    assert "retry_fn" in captured, "CRAG retry was never wired"

    sources_seen.clear()
    captured["retry_fn"]("docker deployment conventions canonical truth")
    assert sources_seen, "retry never searched"
    assert all(s == ["canonical"] for s in sources_seen), sources_seen
