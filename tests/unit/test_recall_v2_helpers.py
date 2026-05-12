"""Unit tests for recall_v2's smaller in-route service helpers.

Companion to test_recall_v2_cache_key.py — each new helper extracted from
the 803-line recall_v2 handler gets pinned here so the next stage of the
refactor verifies no behavior change.
"""

from __future__ import annotations

import sys
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


# ── _merge_source_timing ────────────────────────────────────────────────


def test_merge_source_timing_empty_payloads_leaves_timing_unchanged():
    from routes.recall import _merge_source_timing

    timing: dict = {"search_ms": 42}
    _merge_source_timing(timing, [])
    assert timing == {"search_ms": 42}


def test_merge_source_timing_single_payload_writes_each_source():
    from routes.recall import _merge_source_timing

    timing: dict = {}
    _merge_source_timing(timing, [{"source_timing": {"rag_ms": 30, "canonical_ms": 12}}])
    assert timing == {"rag_ms": 30, "canonical_ms": 12}


def test_merge_source_timing_takes_max_across_payloads():
    """Multiple variants run in parallel — for the same source key, keep
    the SLOWEST since wall-clock for that source is the max across variants.
    This is the core invariant the merger pins."""
    from routes.recall import _merge_source_timing

    timing: dict = {}
    payloads = [
        {"source_timing": {"rag_ms": 30, "canonical_ms": 12}},
        {"source_timing": {"rag_ms": 45, "canonical_ms": 9}},  # rag is slower in 2nd
        {"source_timing": {"rag_ms": 20, "canonical_ms": 25}},  # canonical slower in 3rd
    ]
    _merge_source_timing(timing, payloads)
    assert timing == {"rag_ms": 45, "canonical_ms": 25}


def test_merge_source_timing_payload_without_source_timing_key_is_skipped():
    """search_unified.search_all returns a dict that either has
    source_timing as a dict or omits the key entirely. Both cases must
    leave the timing accumulator unchanged. (A pre-existing edge case is
    `source_timing: None` — that would AttributeError because the prior
    inline code did the same .items() call without a None guard.
    Not patching here to keep the extraction byte-equal to the original.)
    """
    from routes.recall import _merge_source_timing

    timing: dict = {"search_ms": 100}
    _merge_source_timing(timing, [{}, {"results": []}, {"source_timing": {}}])
    assert timing == {"search_ms": 100}


def test_merge_source_timing_preserves_existing_keys_when_new_value_smaller():
    """If timing already has rag_ms=50 and a payload contributes rag_ms=30,
    the existing higher value must be preserved (we keep MAX, not last-write)."""
    from routes.recall import _merge_source_timing

    timing: dict = {"rag_ms": 50}
    _merge_source_timing(timing, [{"source_timing": {"rag_ms": 30}}])
    assert timing == {"rag_ms": 50}


def test_merge_source_timing_overwrites_when_new_value_larger():
    from routes.recall import _merge_source_timing

    timing: dict = {"rag_ms": 10}
    _merge_source_timing(timing, [{"source_timing": {"rag_ms": 100}}])
    assert timing == {"rag_ms": 100}


def test_merge_source_timing_returns_none_and_mutates_in_place():
    from routes.recall import _merge_source_timing

    timing: dict = {}
    result = _merge_source_timing(timing, [{"source_timing": {"x_ms": 1}}])
    assert result is None
    assert timing == {"x_ms": 1}
