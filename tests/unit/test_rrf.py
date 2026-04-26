"""Unit tests for brain_core.rrf — Reciprocal Rank Fusion."""

from __future__ import annotations

import pytest
from rrf import DEFAULT_K, rrf_fuse


def test_empty_lists_returns_empty():
    assert rrf_fuse([]) == []


def test_single_source_preserves_rank_order():
    docs = [
        {"path": "/a", "score": 90},
        {"path": "/b", "score": 50},
        {"path": "/c", "score": 30},
    ]
    fused = rrf_fuse([docs])
    assert [d["path"] for d in fused] == ["/a", "/b", "/c"]
    assert fused[0]["rrf_score"] > fused[1]["rrf_score"] > fused[2]["rrf_score"]


def test_multi_source_consensus_ranks_higher_than_single_source_top():
    rag = [{"path": "/a"}, {"path": "/b"}, {"path": "/c"}]
    canon = [{"path": "/b"}, {"path": "/d"}]
    obs = [{"path": "/b"}, {"path": "/e"}]
    fused = rrf_fuse([rag, canon, obs])
    assert fused[0]["path"] == "/b", "consensus doc should rank first"


def test_rrf_clears_decay_applied_so_downstream_decay_can_re_run():
    """RRF replaces score with rank-based score. The shallow copy preserved
    _decay_applied which made expand/hyde queries effectively skip time
    decay because the downstream apply_to_results no-op'd on the flag.
    """
    docs = [
        {"path": "/a", "score": 90, "_decay_applied": True, "_rerank_applied": True},
        {"path": "/b", "score": 50, "_decay_applied": True, "_rerank_applied": True},
    ]
    fused = rrf_fuse([docs])
    for r in fused:
        assert "_decay_applied" not in r, "RRF must clear _decay_applied so decay re-runs on the new score"
        # _rerank_applied is intentionally preserved — re-applying token-overlap
        # rerank against the rank-based RRF score would compound trust boosts
        # (see recall.py:709 comment from the 2026-04-16 fix).
        assert r["_rerank_applied"] is True
        assert r["score"] == r["rrf_score"]


def test_trust_weights_break_ties():
    src1 = [{"path": "/x"}]
    src2 = [{"path": "/y"}]
    fused = rrf_fuse([src1, src2], trust_weights=[1.0, 0.5])
    by_id = {d["path"]: d["rrf_score"] for d in fused}
    assert by_id["/x"] > by_id["/y"]


def test_trust_weights_length_mismatch_raises():
    with pytest.raises(ValueError, match="length"):
        rrf_fuse([[{"path": "/a"}], [{"path": "/b"}]], trust_weights=[1.0])


def test_anonymous_docs_fuse_by_content_hash():
    body = "the same body text"
    src1 = [{"content": body}]
    src2 = [{"content": body}]
    fused = rrf_fuse([src1, src2])
    assert len(fused) == 1, "identical content should fuse into one doc"


def test_default_k_matches_paper():
    assert DEFAULT_K == 60


def test_score_field_mirrors_rrf_score():
    docs = [{"path": "/a"}, {"path": "/b"}]
    fused = rrf_fuse([docs])
    for d in fused:
        assert d["score"] == d["rrf_score"]


def test_rrf_score_normalized_to_0_100_range():
    docs = [{"path": f"/{i}"} for i in range(5)]
    fused = rrf_fuse([docs])
    for d in fused:
        assert 0.0 <= d["rrf_score"] <= 100.0


def test_caller_dicts_not_mutated():
    original = {"path": "/a", "title": "Original"}
    rrf_fuse([[original]])
    assert "rrf_score" not in original, "fuse must not mutate caller's docs"
