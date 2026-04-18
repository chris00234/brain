"""Behavior tests for 10 heavyweight brain_core modules.

These are the big ones — openclaw_dispatch, memory_lifecycle, indexer,
etc. Each tested at the contract level: import safely, key helpers
return expected types, no hidden runtime dependencies on live services.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core" / "pipeline"))


# ── openclaw_dispatch ────────────────────────────────────────────
def test_openclaw_dispatch_get_usage_stats_shape():
    from openclaw_dispatch import get_usage_stats

    stats = get_usage_stats(days=1)
    assert isinstance(stats, dict)


def test_openclaw_dispatch_purge_returns_int():
    from openclaw_dispatch import purge_old_usage

    # Purging with huge cutoff should return 0 (nothing old enough)
    n = purge_old_usage(days=100000)
    assert isinstance(n, int)
    assert n == 0


# ── memory_lifecycle ─────────────────────────────────────────────
def test_memory_lifecycle_get_memory_files_list():
    from memory_lifecycle import get_memory_files

    files = get_memory_files()
    assert isinstance(files, list)
    # Each entry is a (path, agent, date) tuple
    for entry in files[:3]:
        assert isinstance(entry, tuple)
        assert len(entry) == 3


def test_memory_lifecycle_get_archived_files_list():
    from memory_lifecycle import get_archived_files

    files = get_archived_files()
    assert isinstance(files, list)


# ── indexer ──────────────────────────────────────────────────────
def test_indexer_set_lora_adapter_disable():
    from indexer import set_lora_adapter

    # Setting to None should disable the LoRA adapter
    result = set_lora_adapter(None)
    assert isinstance(result, dict)


# ── reasoning_loop ───────────────────────────────────────────────
def test_reasoning_loop_strip_json_fence():
    from reasoning_loop import _strip_json_fence

    assert _strip_json_fence('```json\n{"a":1}\n```') == '{"a":1}'
    assert _strip_json_fence('```\n[1,2,3]\n```') == '[1,2,3]'
    assert _strip_json_fence('{"a":1}') == '{"a":1}'


# ── autonomy_proposer ────────────────────────────────────────────
def test_autonomy_proposer_fetch_outcomes_returns_list():
    from autonomy_proposer import _fetch_kind_outcomes

    rows = _fetch_kind_outcomes()
    assert isinstance(rows, list)


def test_autonomy_proposer_run_returns_dict():
    """run() is the main cron entry point — always returns a status dict."""
    from autonomy_proposer import run

    r = run()
    assert isinstance(r, dict)


# ── agent_messenger ──────────────────────────────────────────────
def test_agent_messenger_route_to_default():
    from agent_messenger import route_message

    # Unknown agent name routes to a default
    result = route_message({"to": "no_such_agent", "content": "hi", "from_agent": "test"})
    assert isinstance(result, str)


# ── maintenance ──────────────────────────────────────────────────
def test_maintenance_rotate_logs_returns_dict():
    from maintenance import rotate_logs

    r = rotate_logs()
    assert isinstance(r, dict)


def test_maintenance_check_chroma_integrity_dict():
    """Chroma may be up or down — either way we must get a dict (no raise)."""
    from maintenance import check_chroma_integrity

    r = check_chroma_integrity()
    assert isinstance(r, dict)


# ── entity_graph ─────────────────────────────────────────────────
def test_entity_graph_use_neo4j_returns_bool():
    from entity_graph import _use_neo4j

    r = _use_neo4j()
    assert isinstance(r, bool)


# ── learn ────────────────────────────────────────────────────────
def test_learn_digest_deterministic():
    from learn import _digest

    a = _digest("hello world")
    b = _digest("hello world")
    c = _digest("DIFFERENT")
    assert a == b
    assert a != c
    # Digest is a short hex string
    assert isinstance(a, str)
    assert len(a) >= 8


def test_learn_jaccard_bounds():
    from learn import _jaccard

    assert _jaccard(set(), set()) == 0.0
    assert _jaccard({"a"}, {"a"}) == 1.0
    assert 0 < _jaccard({"a", "b"}, {"a", "c"}) < 1


def test_learn_tokenize_basic():
    from learn import _tokenize

    t = _tokenize("Hello, world! Brain-system 123")
    assert isinstance(t, set)
    assert "hello" in t or "world" in t


# ── hyde ─────────────────────────────────────────────────────────
def test_hyde_ttl_cache_basic():
    from hyde import _TTLCache

    c = _TTLCache(ttl_seconds=60, max_entries=3)
    c.set("key1", "value1")
    assert c.get("key1") == "value1"
    assert c.get("missing") is None


def test_hyde_ttl_cache_eviction():
    from hyde import _TTLCache

    c = _TTLCache(ttl_seconds=60, max_entries=2)
    c.set("a", 1)
    c.set("b", 2)
    c.set("c", 3)  # evicts oldest
    stored = sum(1 for k in ("a", "b", "c") if c.get(k) is not None)
    assert stored == 2


def test_hyde_clean_reply_strips_fences():
    from hyde import _clean_reply

    assert "```" not in _clean_reply("```json\nhello\n```")
    assert "```" not in _clean_reply("```\nhello\n```")
