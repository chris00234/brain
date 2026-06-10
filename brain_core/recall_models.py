"""Pydantic request/response models for recall routes.

Kept outside ``routes.recall`` so endpoint orchestration can evolve without
mixing transport schemas, caches, and retrieval governance in one giant module.
``routes.recall`` re-exports these names for backwards-compatible imports.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RecallResultMetadata(BaseModel):
    agent: str | None = None
    service: str | None = None
    type: str | None = None
    domain: str | None = None
    confidence: float | None = None
    review_state: str | None = None
    vector_score: float | None = None
    keyword_score: float | None = None
    id: str | None = None


class RecallResult(BaseModel):
    model_config = {"extra": "allow"}

    score: float
    source_type: str = ""
    collection: str = ""
    title: str = ""
    content: str = ""
    path: str = ""
    trust_tier: int = 1
    metadata: dict[str, Any] = Field(default_factory=dict)


class RecallResponse(BaseModel):
    query: str
    results: list[RecallResult]
    sources_searched: list[str]
    total_candidates: int
    temporal_range: dict | None = None
    expanded_query: str | None = None


class RecallV2Response(BaseModel):
    query: str
    results: list[dict[str, Any]]
    total_candidates: int
    hyde_used: bool = False
    hypothetical: str | None = None
    variants: list[str] = Field(default_factory=list)
    rerank_applied: bool = True
    time_decay_applied: bool = True
    latency_ms: int = 0
    timing: dict[str, Any] = Field(default_factory=dict)
    # 2026-04-17 Phase 4: proactive metacognitive note. Populated only
    # when the top-1 result triggers an uncertainty heuristic (low
    # confidence, pending contradictions, tied top-K, no trusted
    # alternatives). None / absent when the brain is confident — keeps
    # high-trust recall responses clean.
    meta_note: str | None = None


class InjectionBlockModel(BaseModel):
    id: str
    title: str
    content: str
    source: str
    score: float
    priority: str
    path: str | None = None
    memory_id: str | None = None
    include_reason: str | None = None
    token_estimate: int | None = None
    freshness: str | None = None
    risk_flags: list[str] = Field(default_factory=list)
    compiler_score: float | None = None
    contract_category: str | None = None


class RecallActiveRequest(BaseModel):
    """Per-turn active recall payload."""

    prompt: str = Field(..., max_length=8000)
    session_id: str = Field(default="anon", max_length=128)
    turn_idx: int = Field(default=0, ge=0, le=100000)
    agent: str = Field(default="claude", max_length=32)
    cwd: str | None = Field(default=None, max_length=512)
    seen_hashes: list[str] | None = Field(default=None, max_length=200)


class RecallActiveResponse(BaseModel):
    blocks: list[InjectionBlockModel] = Field(default_factory=list)
    intent: str | None = None
    total_tokens: int = 0
    latency_ms: int = 0
    new_since_last_turn: bool = False
    quality: dict = Field(default_factory=dict)
    degraded: bool = False


class SearchFeedbackRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    result_id: str = Field(..., min_length=1, max_length=200)
    result_source: str = Field(default="", max_length=64)
    useful: bool
    # Forward-compat: agent identity for per-agent preference learning.
    # Pre-2026-04 entries lack this field and are treated as agent="system"
    # by feedback_aggregator.
    agent: str = Field(default="system", max_length=32)
    # Phase 7: eval auto-growth signal. When wrong_answer=true and `expected`
    # is set, the query is appended to eval_proposals for weekly review.
    wrong_answer: bool = Field(default=False)
    synthetic: bool = Field(default=False)
    expected: str = Field(default="", max_length=2000)


class RecallBatchRequest(BaseModel):
    queries: list[str] = Field(..., max_length=20, min_length=1)
    n: int = Field(default=5, ge=1, le=20)
    rerank: bool = True
    decay: bool = True
    agent: str = Field(default="unknown", max_length=64)


class CompoundOp(BaseModel):
    """One operation in a transactional compound brain request."""

    # 2026-05-20 W3.5 round-4 defect D: cap op string length so a malformed
    # request can't push an unbounded value into the action_audit query_text
    # via the rejection path.
    op: str = Field(..., max_length=64, description="One of: search, remember, correct, feedback")
    args: dict = Field(default_factory=dict)


class CompoundRequest(BaseModel):
    ops: list[CompoundOp] = Field(..., min_length=1, max_length=10)
    actor: str | None = Field(default=None, description="Calling agent (audit)")
