"""Contract tests for the /recall/v2 + /recall/active response shapes.

These tests don't exercise the actual route handlers (which require
Qdrant/Ollama/Neo4j running). They pin the Pydantic response models
that the public API exposes, so the upcoming recall_v2 803-line
service-layer extraction can verify no field is dropped or retyped
during the refactor.

This is the cheapest tier of protection — it catches:
  - Field rename
  - Field removal
  - Required→optional flip (or vice versa)
  - Default value drift
  - List default sharing bugs (defaults must use Field(default_factory=list))
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


# ── RecallV2Response ─────────────────────────────────────────────────────


def test_recall_v2_response_minimal_payload_validates():
    from routes.recall import RecallV2Response

    resp = RecallV2Response(query="hello", results=[], total_candidates=0)
    assert resp.query == "hello"
    assert resp.results == []
    assert resp.total_candidates == 0
    # Defaults from the model definition (pin them):
    assert resp.hyde_used is False
    assert resp.hypothetical is None
    assert resp.variants == []
    assert resp.rerank_applied is True
    assert resp.time_decay_applied is True
    assert resp.latency_ms == 0
    assert resp.timing == {}
    assert resp.meta_note is None


def test_recall_v2_response_full_payload_round_trips():
    """Dump → reload via .model_validate must be lossless across all fields."""
    from routes.recall import RecallV2Response

    src = RecallV2Response(
        query="capital of france",
        results=[{"id": "atm_a", "content": "Paris", "score": 0.95}],
        total_candidates=1,
        hyde_used=True,
        hypothetical="The capital of France is Paris.",
        variants=["france capital", "paris france"],
        rerank_applied=False,
        time_decay_applied=False,
        latency_ms=123,
        timing={"qdrant_ms": 45, "rerank_ms": 78},
        meta_note="low-confidence top-1",
    )
    dumped = src.model_dump()
    reloaded = RecallV2Response.model_validate(dumped)
    assert reloaded == src


def test_recall_v2_response_results_accepts_arbitrary_dict_shapes():
    """results: list[dict[str, Any]] — must allow any dict shape per result."""
    from routes.recall import RecallV2Response

    resp = RecallV2Response(
        query="x",
        results=[
            {"id": "1", "score": 0.9},  # minimal
            {  # full shape (canonical hit)
                "id": "2",
                "content": "long content",
                "score": 0.85,
                "source": "canonical",
                "path": "canonical/_profile.md",
                "metadata": {"agent": "jenna"},
            },
        ],
        total_candidates=2,
    )
    assert len(resp.results) == 2
    assert resp.results[0]["id"] == "1"
    assert resp.results[1]["path"] == "canonical/_profile.md"


def test_recall_v2_response_variants_default_is_unique_list_per_instance():
    """List defaults must use Field(default_factory=list) so two instances
    don't share the same list object — appending to one would mutate both."""
    from routes.recall import RecallV2Response

    a = RecallV2Response(query="a", results=[], total_candidates=0)
    b = RecallV2Response(query="b", results=[], total_candidates=0)
    a.variants.append("dup")
    assert b.variants == [], "default list is shared across instances — Field(default_factory=list) missing"


def test_recall_v2_response_missing_required_raises():
    from routes.recall import RecallV2Response

    with pytest.raises(ValidationError):
        RecallV2Response()  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        RecallV2Response(query="x")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        RecallV2Response(query="x", results=[])  # type: ignore[call-arg]


# ── RecallResponse (legacy /recall) ──────────────────────────────────────


def test_recall_response_minimal_payload_validates():
    from routes.recall import RecallResponse

    resp = RecallResponse(query="x", results=[], sources_searched=[], total_candidates=0)
    assert resp.temporal_range is None
    assert resp.expanded_query is None


def test_recall_result_default_metadata_is_unique_per_instance():
    from routes.recall import RecallResult

    a = RecallResult(score=0.5)
    b = RecallResult(score=0.5)
    a.metadata["k"] = "v"
    assert b.metadata == {}, "shared dict default — Field(default_factory=dict) missing"


# ── InjectionBlockModel (active recall) ─────────────────────────────────


def test_injection_block_minimal_payload_validates():
    from routes.recall import InjectionBlockModel

    blk = InjectionBlockModel(
        id="b_1",
        title="t",
        content="c",
        source="canonical",
        score=0.9,
        priority="high",
    )
    # Optional fields default to None / []
    assert blk.path is None
    assert blk.memory_id is None
    assert blk.include_reason is None
    assert blk.token_estimate is None
    assert blk.freshness is None
    assert blk.risk_flags == []
    assert blk.compiler_score is None
    assert blk.contract_category is None


def test_injection_block_risk_flags_default_is_unique():
    from routes.recall import InjectionBlockModel

    a = InjectionBlockModel(id="a", title="t", content="c", source="s", score=0, priority="medium")
    b = InjectionBlockModel(id="b", title="t", content="c", source="s", score=0, priority="medium")
    a.risk_flags.append("flag")
    assert b.risk_flags == [], "shared list default in InjectionBlockModel.risk_flags"


# ── RecallActiveRequest / Response ──────────────────────────────────────


def test_recall_active_request_prompt_required_and_length_bounded():
    from routes.recall import RecallActiveRequest

    req = RecallActiveRequest(prompt="hi")
    assert req.session_id == "anon"
    assert req.turn_idx == 0
    assert req.agent == "claude"
    assert req.cwd is None
    assert req.seen_hashes is None

    with pytest.raises(ValidationError):
        RecallActiveRequest()  # type: ignore[call-arg]

    # max_length=8000 on prompt
    with pytest.raises(ValidationError):
        RecallActiveRequest(prompt="x" * 8001)


def test_recall_active_response_defaults():
    from routes.recall import RecallActiveResponse

    resp = RecallActiveResponse()
    assert resp.blocks == []
    assert resp.intent is None
    assert resp.total_tokens == 0
    assert resp.latency_ms == 0
    assert resp.new_since_last_turn is False
    assert resp.quality == {}
    assert resp.degraded is False
