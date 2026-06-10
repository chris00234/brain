"""Shared recall-governance layer.

One query-intent contract, one source-authority model, and one route-guarantee
mechanism, consumed by ``/recall/v2`` (brain_core.routes.recall), ``/recall/active``
(brain_core.active_recall), and Hermes provider prefetch with different
strictness profiles. Pure stdlib + PyYAML; importing this package never pulls in
Chroma/Ollama/Neo4j and never imports the recall route modules, so it is a safe
leaf dependency for every surface (no circular imports).
"""

from __future__ import annotations

from .normalization import normalize_separators, normalize_text, tokenize
from .prefetch_policy import RecallPolicy, policy_for
from .query_analyzer import (
    QueryIntent,
    analyze_query,
    is_incident_retrospective_query,
    is_live_state_query,
    is_out_of_domain_world_knowledge_query,
    is_positive_summary_intent_query,
    is_summary_excluded_query,
    query_targets_openclaw_or_agents,
)
from .route_guarantees import (
    RouteGuarantee,
    guarantee_tokens,
    is_declarative_route_guarantee,
    match_route_guarantees,
    matched_route_tags,
)
from .source_authority import (
    AuthorityTier,
    classify_result,
    is_distilled_brain_analysis_result,
    is_durable_truth_result,
    is_episodic_event_log_result,
    is_generic_summary_result,
    is_generic_summary_title,
    is_low_authority_block,
    is_low_authority_result,
    is_openclaw_historical_result,
    is_query_keyed_bridge_result,
    is_source_or_test_file_result,
    is_vanished_source_result,
    result_category,
    result_metadata,
    result_text,
)

__all__ = [
    "AuthorityTier",
    "QueryIntent",
    "RecallPolicy",
    "RouteGuarantee",
    "analyze_query",
    "classify_result",
    "guarantee_tokens",
    "is_declarative_route_guarantee",
    "is_distilled_brain_analysis_result",
    "is_durable_truth_result",
    "is_episodic_event_log_result",
    "is_generic_summary_result",
    "is_generic_summary_title",
    "is_incident_retrospective_query",
    "is_live_state_query",
    "is_low_authority_block",
    "is_low_authority_result",
    "is_openclaw_historical_result",
    "is_out_of_domain_world_knowledge_query",
    "is_positive_summary_intent_query",
    "is_query_keyed_bridge_result",
    "is_source_or_test_file_result",
    "is_summary_excluded_query",
    "is_vanished_source_result",
    "match_route_guarantees",
    "matched_route_tags",
    "normalize_separators",
    "normalize_text",
    "policy_for",
    "query_targets_openclaw_or_agents",
    "result_category",
    "result_metadata",
    "result_text",
    "tokenize",
]
