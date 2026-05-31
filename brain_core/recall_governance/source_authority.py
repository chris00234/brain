"""Shared source-authority contract for the recall-governance layer.

The recall corpus mixes direct durable truth (semantic memories, accepted
canonical facts/preferences/decisions, route guarantees) with derived/secondary
formats (summaries, reflections, session/weekly digests, procedure/voyager
logs, distilled brain-analysis meta) and episodic event logs. For any query not
explicitly asking for a summary, the former should outrank the latter.

These classifiers express that contract from PROVENANCE signals (collection /
category / review_state / doc format / path) — never topic markers — so ONE
rule serves every recall class instead of a per-probe boost/penalty pair.
Consumed by ``/recall/v2`` governance, ``/recall/active`` block filtering, and
Hermes provider prefetch. Pure stdlib; no IO at import; no circular deps.
"""

from __future__ import annotations

import re
from enum import IntEnum
from typing import Any


class AuthorityTier(IntEnum):
    """Lower is more authoritative. Direct current truth outranks everything;
    obsolete/superseded ranks last."""

    DIRECT_CURRENT_TRUTH = 1
    CURATED_CANONICAL = 2
    DERIVED_SUMMARY = 3
    EPISODIC_LOG = 4
    SOURCE_OR_TEST_QUOTE = 5
    OBSOLETE_OR_SUPERSEDED = 6


_TRUTH_CATEGORIES = {"preference", "decision", "correction", "fact"}

_GENERIC_SUMMARY_MARKERS = (
    "weekly",
    "week ",
    "brain summary",
    "session summary",
    "summary (",
    "raptor",
    "summaries",
)
_LOW_AUTHORITY_PROVENANCE_MARKERS = (
    "brain-reflect",
    "brain_reflect",
    "/reflect",
    "reflection",
    "nightly",
    "/sessions/",
    "session_summary",
    "session-summary",
    "/weekly",
    "weekly_",
    "/summaries/",
    "/procedures/",
    "procedure",
    "voyager",
    "raptor",
)
_EPISODIC_LOG_TITLE_PREFIXES = (
    "### details",
    "### context",
    "### suggested action",
    "## suggested action",
    "### error",
    "coding_event:",
)
_EPISODIC_EVENT_COLLECTIONS = {"raw_events", "raw_event"}
# Distillation 'Summary' format: a row whose CONTENT leads with a markdown
# Summary header ("# Summary …", "## Summary …", possibly wrapping a JSON
# envelope) is a derived distillation-format artifact — the same secondary-format
# contract as a title-level Summary — even when stored in a durable collection
# (canonical/semantic). Format/provenance signal, not a topic marker.
_SUMMARY_CONTENT_HEADER_RE = re.compile(r"(?is)^\s*#{1,3}\s*summary\b")
_SOURCE_CODE_PATH_SUFFIXES = (
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".go",
    ".rs",
    ".java",
    ".sh",
    ".sql",
    ".css",
    ".cpp",
    ".c",
    ".rb",
)


# ── Result field accessors ────────────────────────────────────────────────


def result_metadata(result: dict) -> dict[str, Any]:
    meta = result.get("metadata")
    return meta if isinstance(meta, dict) else {}


def result_category(result: dict) -> str:
    meta = result_metadata(result)
    value = meta.get("category") or meta.get("type") or result.get("category") or result.get("type") or ""
    return str(value).lower()


def result_text(result: dict) -> str:
    meta = result_metadata(result)
    return " ".join(
        str(part or "")
        for part in (
            result.get("title"),
            result.get("path"),
            result.get("content"),
            meta.get("title"),
            meta.get("path"),
            meta.get("id"),
            meta.get("source_path"),
            meta.get("source_name"),
            meta.get("document_title"),
            meta.get("document_section"),
            meta.get("document_type"),
        )
    )


# ── Provenance/format classifiers ─────────────────────────────────────────


def is_generic_summary_result(result: dict) -> bool:
    meta = result_metadata(result)
    title = str(result.get("title") or meta.get("title") or "").strip().lower()
    haystack = " ".join(
        str(part or "")
        for part in (
            result.get("title"),
            result.get("path"),
            result.get("type"),
            result.get("source_type"),
            meta.get("title"),
            meta.get("path"),
            meta.get("type"),
            meta.get("source_type"),
            meta.get("source_path"),
            meta.get("source_name"),
            meta.get("document_title"),
        )
    ).lower()
    return (
        title in {"summary", "brain summary", "session summary"}
        or any(marker in haystack for marker in _GENERIC_SUMMARY_MARKERS)
        or bool(re.search(r"\bw\d{1,2}\b", haystack))
    )


def is_distilled_brain_analysis_result(result: dict, text: str) -> bool:
    meta = result_metadata(result)
    lower = text.lower()
    meta_text = " ".join(
        str(meta.get(key) or "") for key in ("id", "subtype", "source_path", "source_name", "document_type")
    ).lower()
    title = str(result.get("title") or meta.get("document_title") or "").strip().lower()
    collection = str(result.get("collection") or "").lower()
    return (
        str(meta.get("subtype") or "").lower() == "brain-analysis"
        or "dist_brain_analysis" in lower
        or '"subtype": "brain-analysis"' in lower
        or "dist_brain_analysis" in meta_text
        or "brain-analysis" in meta_text
        or (collection in {"canonical", "distilled"} and title == "reasoning")
    )


def is_episodic_event_log_result(result: dict, text: str) -> bool:
    """True for episodic event/coding-session captures (raw coding-events, or
    agent-session logs with ### Details / Context / Suggested Action / Error
    scaffolds). Provenance/shape only — never topic markers."""
    meta = result_metadata(result)
    rid = str(result.get("id") or meta.get("id") or "").lower()
    source_type = str(result.get("source_type") or meta.get("source_type") or "").lower()
    collection = str(result.get("collection") or "").lower()
    if rid.startswith(("raw_coding_event", "coding_event")) or source_type in {"coding_event", "raw_event"}:
        return True
    if collection in _EPISODIC_EVENT_COLLECTIONS:
        return True
    if collection in {"experience", "patterns"}:
        title = (
            str(result.get("title") or meta.get("document_title") or meta.get("title") or "").strip().lower()
        )
        return any(title.startswith(p) for p in _EPISODIC_LOG_TITLE_PREFIXES)
    return False


def is_source_or_test_file_result(result: dict) -> bool:
    """True when a row's provenance is a source-code or test file. For
    out-of-domain world-knowledge prompts these only match by quoting the query
    (e.g. a probe string written into a test), never by answering it."""
    meta = result_metadata(result)
    path = str(result.get("path") or meta.get("source_path") or meta.get("path") or "").lower()
    title = str(result.get("title") or meta.get("document_title") or "").lower()
    if path.endswith(_SOURCE_CODE_PATH_SUFFIXES) or title.endswith(_SOURCE_CODE_PATH_SUFFIXES):
        return True
    return "/tests/" in path or "test_" in path or "test_" in title


def is_low_authority_result(result: dict, text: str) -> bool:
    """True for derived/secondary-format rows (the penalized half of the contract).

    Composes the summary and distilled-brain-analysis classifiers with episodic
    event/coding-session logs and reflection / session / weekly / procedure /
    voyager provenance markers, plus the distillation 'Summary'-shaped content
    format (a row whose content leads with a '# Summary' header).
    """
    if (
        is_generic_summary_result(result)
        or is_distilled_brain_analysis_result(result, text)
        or is_episodic_event_log_result(result, text)
        or _SUMMARY_CONTENT_HEADER_RE.match(str(result.get("content") or ""))
    ):
        return True
    meta = result_metadata(result)
    title = str(result.get("title") or meta.get("document_title") or meta.get("title") or "").strip().lower()
    if title in {"reasoning", "recap", "digest"} or title.startswith("### summary"):
        return True
    haystack = " ".join(
        str(part or "")
        for part in (
            result.get("id"),
            result.get("path"),
            result.get("collection"),
            meta.get("id"),
            meta.get("subtype"),
            meta.get("document_type"),
            meta.get("type"),
            meta.get("source_path"),
            meta.get("source_name"),
        )
    ).lower()
    return any(marker in haystack for marker in _LOW_AUTHORITY_PROVENANCE_MARKERS)


def is_durable_truth_result(result: dict) -> bool:
    """True for direct durable memory (the boosted half of the contract).

    Semantic memories, accepted canonical rows, or any truth-category
    (preference/decision/fact/correction) row — as long as it is not marked
    superseded/expired/obsolete. A durable COLLECTION does not make a derived
    FORMAT durable: a Summary/brain-analysis/procedure-shaped row is
    low-authority even when stored in semantic_memory/canonical, so reject those
    first.
    """
    meta = result_metadata(result)
    review_state = str(meta.get("review_state") or result.get("review_state") or "").lower()
    if review_state in {"superseded", "expired", "obsolete", "rejected", "deprecated"}:
        return False
    if meta.get("expired") or meta.get("obsolete") or result.get("expired"):
        return False
    if is_low_authority_result(result, result_text(result)):
        return False
    collection = str(result.get("collection") or "").lower()
    category = result_category(result)
    return (
        collection == "semantic_memory"
        or (collection == "canonical" and review_state in {"accepted", "approved", "canonical"})
        or category in _TRUTH_CATEGORIES
    )


def _is_obsolete_result(result: dict) -> bool:
    meta = result_metadata(result)
    review_state = str(meta.get("review_state") or result.get("review_state") or "").lower()
    if review_state in {"superseded", "expired", "obsolete", "rejected", "deprecated"}:
        return True
    return bool(meta.get("expired") or meta.get("obsolete") or result.get("expired"))


def classify_result(result: dict) -> AuthorityTier:
    """Map a result row to its :class:`AuthorityTier` from provenance alone.

    Order matters: obsolete first, then direct truth, then quoting-only source/
    test files and episodic/derived logs, then curated canonical, with a neutral
    curated default for unclassified rows.
    """
    if _is_obsolete_result(result):
        return AuthorityTier.OBSOLETE_OR_SUPERSEDED
    if is_durable_truth_result(result):
        return AuthorityTier.DIRECT_CURRENT_TRUTH
    text = result_text(result)
    if is_source_or_test_file_result(result):
        return AuthorityTier.SOURCE_OR_TEST_QUOTE
    if is_episodic_event_log_result(result, text):
        return AuthorityTier.EPISODIC_LOG
    if is_low_authority_result(result, text):
        return AuthorityTier.DERIVED_SUMMARY
    return AuthorityTier.CURATED_CANONICAL


# ── Historical-runtime provenance ─────────────────────────────────────────
# OpenClaw is historical context; Hermes is the current agent runtime (the
# durable runtime_distinction route guarantee). A row whose text/path is
# dominated by OpenClaw provenance restates migration-era context. For the
# strictest surface (provider prefetch, "empty beats wrong"), such a row should
# not be injected unless the prompt is actually about OpenClaw/the agents.
# Topic/provenance signal (EN+KO), not a per-probe marker list.
_OPENCLAW_PROVENANCE_MARKERS = ("openclaw", "오픈클로")


def is_openclaw_historical_result(result: dict, text: str | None = None) -> bool:
    """True when a row's provenance/content marks it as historical OpenClaw-era
    context (OpenClaw session/workspace captures, OpenClaw-themed distillations).

    Mirrors the durable runtime_distinction fact (OpenClaw historical, Hermes
    current). Used by the strict provider-prefetch surface to drop stale OpenClaw
    rows for prompts not about OpenClaw/agents — see query_analyzer
    .query_targets_openclaw_or_agents for the symmetric query gate."""
    haystack = (text if text is not None else result_text(result)).lower()
    return any(marker in haystack for marker in _OPENCLAW_PROVENANCE_MARKERS)


# ── Block-level authority (InjectionBlock parity) ─────────────────────────
# Mirror of is_low_authority_result for active-recall InjectionBlocks, which
# carry provenance in title/source/path rather than collection/metadata.

_LOW_AUTHORITY_BLOCK_MARKERS = (
    "/sessions/",
    "session_summary",
    "session-summary",
    "brain-reflect",
    "/reflect",
    "reflection",
    "nightly",
    "/weekly",
    "weekly_",
    "/summaries/",
    "/procedures/",
    "procedure",
    "voyager",
    "raptor",
)


def is_generic_summary_title(title: str) -> bool:
    return bool(re.match(r"(?i)^\s*summary(?:\s*\(part\s*\d+\))?\s*$", title or ""))


def is_low_authority_block(block: Any) -> bool:
    """True for derived/secondary InjectionBlocks (summary/reflection/session/
    procedure). Duck-typed on ``.title``/``.source``/``.path`` (or dict keys)."""

    def _attr(name: str) -> str:
        if isinstance(block, dict):
            return str(block.get(name) or "")
        return str(getattr(block, name, "") or "")

    title = _attr("title")
    if is_generic_summary_title(title):
        return True
    haystack = f"{_attr('source')}\n{title}\n{_attr('path')}".lower()
    return any(marker in haystack for marker in _LOW_AUTHORITY_BLOCK_MARKERS)
