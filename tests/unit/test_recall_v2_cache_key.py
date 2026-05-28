"""Unit tests for routes.recall._build_recall_v2_cache_key.

First service-helper extracted from the 803-line recall_v2 route handler
(2026-05-12). Pure function, no I/O — just deterministic string
concatenation of the request params + session/agent/adapter markers.

These tests pin the exact string format so the next stages of the
extraction can verify the cache key contract is unchanged.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


class _FakeRequest:
    """Minimal Request stand-in — only .headers is touched by the helper."""

    def __init__(self, headers: dict[str, str] | None = None):
        self.headers = headers or {}


@pytest.fixture(autouse=True)
def _clear_lora_adapter(monkeypatch):
    """Force base-adapter marker so the cache-key format is deterministic.
    The helper imports `indexer._lora_embedder` at call time; ensuring it's
    None gives a stable 'emb=base' suffix.
    """
    import indexer

    monkeypatch.setattr(indexer, "_lora_embedder", None, raising=False)


def _default_kwargs():
    """Standard kwargs matching recall_v2's defaults at call site."""
    return dict(
        hyde=False,
        expand=False,
        rerank=True,
        decay=True,
        iterative=False,
        collection=None,
        domain=None,
        agent=None,
        since=None,
        until=None,
        entity=None,
        source_type=None,
        include_history=False,
        include_obsolete=False,
        as_of=None,
        canonical_first=False,
        exclude_already_used=False,
    )


def test_cache_key_baseline_matches_pre_extraction_format():
    """The extracted helper must produce the exact same string as the prior
    inline computation. Anchor: q='hello', n=10, all defaults, no headers,
    no adapter — yields the canonical baseline string."""
    from routes.recall import _build_recall_v2_cache_key

    req = _FakeRequest()
    key = _build_recall_v2_cache_key(req, "hello", 10, **_default_kwargs())
    expected = (
        "hello:10:False:False:True:True:False:None:"
        "None:filter_agent=None:None:None:None:None:"
        "False:False:None:False:"
        "excl=False:"
        "sess=:agent=:emb=base"
    )
    assert key == expected


def test_cache_key_session_id_included():
    """X-Session-Id header must appear in the sess= segment so two
    concurrent sessions with the same query don't collide."""
    from routes.recall import _build_recall_v2_cache_key

    req_a = _FakeRequest({"x-session-id": "sess_a"})
    req_b = _FakeRequest({"x-session-id": "sess_b"})
    key_a = _build_recall_v2_cache_key(req_a, "q", 5, **_default_kwargs())
    key_b = _build_recall_v2_cache_key(req_b, "q", 5, **_default_kwargs())
    assert "sess=sess_a" in key_a
    assert "sess=sess_b" in key_b
    assert key_a != key_b


def test_cache_key_agent_header_included():
    from routes.recall import _build_recall_v2_cache_key

    req_claude = _FakeRequest({"x-agent": "claude"})
    req_jenna = _FakeRequest({"x-agent": "jenna"})
    key_a = _build_recall_v2_cache_key(req_claude, "q", 5, **_default_kwargs())
    key_b = _build_recall_v2_cache_key(req_jenna, "q", 5, **_default_kwargs())
    assert "agent=claude" in key_a
    assert "agent=jenna" in key_b
    assert key_a != key_b


def test_cache_key_adapter_marker_changes_when_lora_active(monkeypatch):
    """When a LoRA adapter is loaded, emb= segment uses the adapter path
    instead of 'base'. Verifies the 2026-04-17 A/B-gate fix."""
    import indexer
    from routes.recall import _build_recall_v2_cache_key

    # Mock the (adapter_path, st_model) tuple shape that _lora_embedder uses
    monkeypatch.setattr(indexer, "_lora_embedder", ("/tmp/adapter_v1", object()), raising=False)
    req = _FakeRequest()
    key = _build_recall_v2_cache_key(req, "q", 5, **_default_kwargs())
    assert "emb=/tmp/adapter_v1" in key
    assert "emb=base" not in key


def test_cache_key_adapter_resolve_failure_falls_back_to_base(monkeypatch):
    """If indexer can't be imported / _lora_embedder is missing, fall back
    to 'base' rather than crash the cache-key build."""
    from routes.recall import _build_recall_v2_cache_key

    # Make the indexer import fail at the call site
    monkeypatch.setitem(sys.modules, "indexer", types.ModuleType("indexer"))
    req = _FakeRequest()
    key = _build_recall_v2_cache_key(req, "q", 5, **_default_kwargs())
    assert "emb=base" in key


def test_cache_key_every_param_changes_output():
    """Changing any single parameter must change the cache key — otherwise
    the cache would silently serve incorrect cross-param hits."""
    from routes.recall import _build_recall_v2_cache_key

    req = _FakeRequest()
    base = _build_recall_v2_cache_key(req, "q", 5, **_default_kwargs())

    flips = [
        ("q",),
        ("hyde", True),
        ("expand", True),
        ("rerank", False),
        ("decay", False),
        ("iterative", True),
        ("collection", "canonical"),
        ("domain", "infra"),
        ("since", "2026-01-01"),
        ("until", "2026-05-01"),
        ("entity", "openclaw"),
        ("source_type", "note"),
        ("include_history", True),
        ("include_obsolete", True),
        ("as_of", "2026-04-01"),
        ("canonical_first", True),
        ("exclude_already_used", True),
    ]
    for f in flips:
        kw = _default_kwargs()
        if f[0] == "q":
            new_key = _build_recall_v2_cache_key(req, "qq", 5, **kw)
        else:
            kw[f[0]] = f[1]
            new_key = _build_recall_v2_cache_key(req, "q", 5, **kw)
        assert new_key != base, f"changing {f[0]} did not change cache key"

    # n also matters
    n_key = _build_recall_v2_cache_key(req, "q", 10, **_default_kwargs())
    assert n_key != base


def test_cache_key_deterministic_for_same_inputs():
    """Calling twice with identical args must produce identical strings —
    the helper must not embed time / random state."""
    from routes.recall import _build_recall_v2_cache_key

    req = _FakeRequest({"x-session-id": "s1", "x-agent": "claude"})
    k1 = _build_recall_v2_cache_key(req, "q", 5, **_default_kwargs())
    k2 = _build_recall_v2_cache_key(req, "q", 5, **_default_kwargs())
    assert k1 == k2
