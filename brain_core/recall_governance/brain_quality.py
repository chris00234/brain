"""Brain recall-quality query/result classifiers.

These helpers identify Brain recall/prefetch quality prompts and stale generic
Brain-system rows. They remain pure functions because they carry no mutable
state; ``routes.recall`` keeps compatibility wrappers where query augmentation is
still route-local.
"""

from __future__ import annotations

from .normalization import tokenize
from .query_analyzer import is_positive_summary_intent_query, is_summary_excluded_query
from .source_authority import is_generic_summary_result, result_text

BRAIN_QUALITY_SUBSYSTEM_TOKENS = {
    "brain",
    "recall",
    "prefetch",
    "retrieval",
    "브레인",
    "리콜",
    "검색품질",
}
BRAIN_QUALITY_BROAD_TOKENS = {
    "context",
    "noise",
    "noisy",
    "eval",
    "evaluation",
    "score",
    "quality",
    "fine",
    "tuning",
    "노이즈",
    "평가",
    "품질",
    "튜닝",
}
BRAIN_QUALITY_GENERIC_MARKERS = (
    "knowledge gap bridge: brain system dependency",
    "brain depends on fastapi brain-server",
    "turning brain and openclaw from clever infrastructure",
    "native qdrant",
    "native ollama",
    "underused tools",
    "brain_decide",
    "search index",
    "qdrant vector store",
    "fastapi server",
    "port 8791",
)


def is_brain_quality_query_text(text: str) -> bool:
    """True when query text names Brain recall/retrieval quality.

    ``routes.recall`` may pass augmented query text so Korean/intent expansions
    remain a route concern while this module owns the token contract.
    """
    if "brain_decide" in (text or "").lower():
        return True
    tokens = tokenize(text)
    return bool(tokens & BRAIN_QUALITY_SUBSYSTEM_TOKENS) and bool(tokens & BRAIN_QUALITY_BROAD_TOKENS)


def is_stale_generic_quality_result(
    result: dict,
    query_text: str,
    *,
    quality_query_text: str | None = None,
) -> bool:
    """True for generic Brain-system rows that are stale for quality prompts.

    ``quality_query_text`` lets callers pass augmented text for query detection
    while preserving raw-query summary and marker semantics.
    """
    if not is_brain_quality_query_text(quality_query_text if quality_query_text is not None else query_text):
        return False
    if is_positive_summary_intent_query(query_text):
        return False
    lower_query = (query_text or "").lower()
    haystack = result_text(result).lower()
    for marker in BRAIN_QUALITY_GENERIC_MARKERS:
        if marker in haystack and marker not in lower_query:
            return True
    # Weekly/session summary blobs are usually stale noise for concrete Brain
    # quality fixes unless the user explicitly asks for a recap.
    return is_generic_summary_result(result) and not is_summary_excluded_query(query_text)
