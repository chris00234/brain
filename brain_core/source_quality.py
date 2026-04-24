"""Source quality calibration for retrieval ranking.

The retrieval stack sees several raw operational logs that are useful as
fallback evidence but poor as direct answers. Keep this policy in one module so
lexical rerank and cross-encoder rerank do not drift.
"""

from __future__ import annotations

from typing import Any

SESSION_DUMP_TYPES = frozenset(
    {
        "raw-openclaw_session",
        "raw-claude_code_session",
        "raw-screen_time",
        "raw-browser",
        "raw-git_activity",
    }
)

DERIVED_MEMORY_TYPES = frozenset(
    {
        "self_learning",
    }
)

AGGREGATE_LEARNING_FILENAMES = frozenset(
    {
        "learnings.md",
        "errors.md",
    }
)


def _metadata(result: dict[str, Any]) -> dict[str, Any]:
    meta = result.get("metadata")
    return meta if isinstance(meta, dict) else {}


def result_doc_type(result: dict[str, Any]) -> str:
    """Return the best available low-cardinality type label."""

    doc_type = result.get("type") or _metadata(result).get("type") or ""
    return str(doc_type).strip().lower()


def result_path(result: dict[str, Any]) -> str:
    """Return the best available source path/source id."""

    meta = _metadata(result)
    path = (
        result.get("path")
        or result.get("source")
        or meta.get("source_path")
        or meta.get("path")
        or meta.get("source")
        or ""
    )
    return str(path)


def is_aggregate_learning_log(result: dict[str, Any]) -> bool:
    """True for giant OpenClaw/Claude learning logs, not ordinary memories."""

    path = result_path(result).replace("\\", "/").lower()
    if "/.learnings/" not in path:
        return False
    return path.rsplit("/", 1)[-1] in AGGREGATE_LEARNING_FILENAMES


def source_quality_multiplier(result: dict[str, Any], *, stage: str) -> float:
    """Return a bounded ranking multiplier for low-quality raw sources.

    `stage="lexical"` is applied before cross-encoder scoring, where raw
    session dumps otherwise flood the candidate window. `stage="cross_encoder"`
    is lighter because the semantic signal is already strong, but it must still
    survive CE's score overwrite.
    """

    doc_type = result_doc_type(result)
    multiplier = 1.0

    if doc_type in SESSION_DUMP_TYPES:
        multiplier *= 0.4 if stage == "lexical" else 0.72

    if doc_type in DERIVED_MEMORY_TYPES:
        multiplier *= 0.85 if stage == "lexical" else 0.72

    if is_aggregate_learning_log(result):
        multiplier *= 0.7 if stage == "lexical" else 0.72

    return multiplier
