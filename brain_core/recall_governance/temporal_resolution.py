"""Read-time entity-property temporal resolution for recall governance.

Two LIVE durable-truth rows can assert conflicting values about the same
subject ("uses X" vs "uses Y", "port 8791" vs "port 9100", enabled vs
disabled) when write-time supersession missed the pair: the ingest cosine
gate treats sim >= 0.85 as restatement and < 0.70 as contradiction, so the
0.70-0.85 window — typical for same-frame value swaps — stores both atoms
with no superseded_by link, and recall then ranks them purely by score
(durable collections get no time decay and no recency tie-break). This module
is the retrieval-time half of the contract: detect the conflicting pair from
STRUCTURE (shared token frame + polarity flip / numeric mismatch /
value-token swap — never topic markers) and report the OLDER row so the route
demotes (never drops) it below the newer durable fact.

Pattern follows Zep/Graphiti soft edge invalidation (arXiv:2501.13956 — a
newer contradicting fact sets the older edge invalid; history stays
queryable) and APEX-MEM retrieval-time temporal resolution (arXiv:2604.14362
— append-only facts, "most recent valid entry" selected at query time, which
beats eager write-time consolidation by 15-25pp on temporal questions).
Divergence heuristics mirror brain_core/conflict_surfacer.py (the nightly
write-side surfacer) so both sides of the contract agree on what "divergent"
means; the sets are kept verbatim here because recall_governance is a leaf
package that must not import sibling brain_core modules.

Known boundary (v1, by design): only created_at (transaction time) orders the
pair — no event-time bi-temporality; and a SUBSUMING newer statement ("Hermes
is current; OpenClaw is historical") whose tokens are a superset of the older
claim does not fire the value-swap signal — that class is already served by
route guarantees. A same-frame verb paraphrase ("prefers X" / "likes X") may
demote the older copy of an equivalent fact; the content still surfaces via
the newer row, so the failure mode is benign reordering, never loss.

Pure stdlib; no IO; safe leaf import for every recall surface.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from .normalization import content_tokens, tokenize
from .source_authority import is_durable_truth_result, result_metadata

# Demotion contract for the route layer: decisive, never drop — same family
# as vanished_source_penalty / query_keyed_bridge_penalty in routes.recall.
TEMPORAL_RESOLUTION_PENALTY = 160.0

# conflict_surfacer parity: same-topic pairs need only modest overlap when a
# HARD divergence signal (polarity flip / numeric mismatch) is present, and a
# length-ratio gate keeps different-scope texts from pairing.
MIN_CONFLICT_OVERLAP = 0.30
MAX_TEXT_LEN_RATIO = 2.0
# A same-frame value swap ("uses vim" -> "uses neovim") has no hard signal,
# so it requires a much tighter frame: high overlap, each side contributing
# only 1-2 exclusive tokens, and enough tokens that the frame is a statement
# rather than a fragment. Equal numeric signatures on both sides VETO the
# swap — matching values corroborate rather than contradict.
MIN_VALUE_SWAP_OVERLAP = 0.55
MAX_VALUE_SWAP_EXCLUSIVE = 2
MIN_VALUE_SWAP_TOKENS = 4
# Pairwise scan window over the fused candidate list. Post-fusion lists are
# small; the cap keeps the worst case bounded and predictable.
MAX_PAIRWISE_WINDOW = 40

_TEMPORAL_HISTORY_PROMPT_TOKENS = frozenset(
    {
        "history",
        "historical",
        "past",
        "timeline",
        "previous",
        "previously",
        "former",
        "formerly",
        "origin",
        "origins",
        "original",
        "originally",
        "provenance",
        "source",
        "sources",
        "trace",
        "traces",
        "when",
        "asof",
        "as-of",
        "역사",
        "과거",
        "이전",
        "예전",
        "출처",
        "유래",
    }
)


def is_temporal_history_prompt(prompt: str) -> bool:
    """True when the query asks for history/provenance rather than current truth.

    Temporal resolution demotes older contradicted facts for current-state recall,
    but history/provenance queries need the original ordering so older rows stay
    visible. Keep this predicate in the leaf governance package so active recall
    and /recall/v2 cannot drift.
    """
    lowered = (prompt or "").lower()
    return (
        bool(tokenize(lowered) & _TEMPORAL_HISTORY_PROMPT_TOKENS) or "as of" in lowered or "as-of" in lowered
    )


# brain_core/conflict_surfacer.py parity (see module docstring on why these
# are duplicated rather than imported). Polarity is computed over the RAW
# token set: "no"/"not" are closed-class function words that content_tokens
# strips, but they are exactly the polarity signal.
_NEGATION_TOKENS = frozenset(
    {
        "no",
        "not",
        "never",
        "without",
        "disabled",
        "deprecated",
        "removed",
        "stopped",
        "retired",
        "broken",
        "false",
    }
)
_AFFIRM_TOKENS = frozenset(
    {
        "yes",
        "enabled",
        "active",
        "working",
        "live",
        "running",
        "true",
        "current",
        "primary",
    }
)

# Date/clock-shaped digit runs are provenance noise, not fact values — a
# durable row often embeds its own capture date, and two true statements
# captured on different days must not read as a numeric conflict. Mirror of
# quality.normalize_recall_signature's date stripping.
_DATE_RE = re.compile(r"\b20\d{2}(?:[-_/.]?w?\d{1,2})?(?:[-_/.]\d{1,2})?\b")
_CLOCK_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def _strip_datetimes(text: str) -> str:
    return _CLOCK_RE.sub(" ", _DATE_RE.sub(" ", text or ""))


def _numeric_signature(text: str) -> frozenset[str]:
    return frozenset(_NUMBER_RE.findall(_strip_datetimes(text)))


def _frame_tokens(text: str) -> set[str]:
    """content_tokens over date/clock-stripped text: capture dates are
    provenance noise for the FRAME as well as for the numeric signature, so
    two true statements captured on different days never read as a value swap.
    """
    return content_tokens(_strip_datetimes(text))


def _polarity(text: str) -> str:
    tokens = tokenize(text)
    neg = bool(tokens & _NEGATION_TOKENS)
    pos = bool(tokens & _AFFIRM_TOKENS)
    if neg and not pos:
        return "negative"
    if pos and not neg:
        return "positive"
    return "neutral"


def _conflicting(text_a: str, toks_a: set[str], text_b: str, toks_b: set[str]) -> bool:
    if not toks_a or not toks_b:
        return False
    len_a, len_b = len(text_a), len(text_b)
    if max(len_a, len_b) > MAX_TEXT_LEN_RATIO * max(min(len_a, len_b), 1):
        return False
    overlap = len(toks_a & toks_b) / len(toks_a | toks_b)
    nums_a, nums_b = _numeric_signature(text_a), _numeric_signature(text_b)
    if overlap >= MIN_CONFLICT_OVERLAP:
        pol_a, pol_b = _polarity(text_a), _polarity(text_b)
        if pol_a != pol_b and "neutral" not in (pol_a, pol_b):
            return True
        if nums_a and nums_b and nums_a != nums_b:
            return True
    return (
        overlap >= MIN_VALUE_SWAP_OVERLAP
        and min(len(toks_a), len(toks_b)) >= MIN_VALUE_SWAP_TOKENS
        and 1 <= len(toks_a - toks_b) <= MAX_VALUE_SWAP_EXCLUSIVE
        and 1 <= len(toks_b - toks_a) <= MAX_VALUE_SWAP_EXCLUSIVE
        and not (nums_a and nums_b and nums_a == nums_b)
    )


def is_conflicting_statement_pair(text_a: str, text_b: str) -> bool:
    """True when two statements share a frame but diverge on the asserted
    value: polarity flip or numeric mismatch inside a modest-overlap frame
    (conflict_surfacer parity), or a 1-2 token value swap inside a tight one.
    """
    return _conflicting(text_a, _frame_tokens(text_a), text_b, _frame_tokens(text_b))


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Naive timestamps read as UTC so aware/naive rows stay comparable.
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _row_timestamp(result: dict) -> datetime | None:
    meta = result_metadata(result)
    for value in (result.get("created_at"), meta.get("created_at")):
        ts = _parse_timestamp(value)
        if ts is not None:
            return ts
    return None


def _row_text(result: dict) -> str:
    return str(result.get("content") or result.get("title") or "")


def _is_resolution_candidate(result: dict) -> bool:
    # Synthetic route-guarantee rows ARE the durable current answer — they are
    # never the stale side of a pair, and they carry no real created_at.
    if str(result.get("source_type") or "").lower() == "route_guarantee":
        return False
    governance = result.get("governance")
    if isinstance(governance, list) and "route_guarantee" in governance:
        return False
    return is_durable_truth_result(result)


def stale_conflict_pairs(
    results: list[dict], *, max_window: int = MAX_PAIRWISE_WINDOW
) -> list[tuple[int, int]]:
    """Return ``(older_index, newer_index)`` pairs of LIVE durable-truth rows
    asserting conflicting values about the same subject.

    Fail-open by construction: rows that are non-durable, route-guarantee
    synthetic, timestamp-less, empty, or timestamp-tied never pair, so the
    caller demotes nothing unless both sides of a conflict are positively
    identified.
    """
    window: list[tuple[int, datetime, str, set[str]]] = []
    for idx, result in enumerate(results[:max_window]):
        if not isinstance(result, dict) or not _is_resolution_candidate(result):
            continue
        ts = _row_timestamp(result)
        if ts is None:
            continue
        text = _row_text(result)
        toks = _frame_tokens(text)
        if not toks:
            continue
        window.append((idx, ts, text, toks))
    pairs: list[tuple[int, int]] = []
    for a in range(len(window)):
        idx_a, ts_a, text_a, toks_a = window[a]
        for b in range(a + 1, len(window)):
            idx_b, ts_b, text_b, toks_b = window[b]
            if ts_a == ts_b:
                continue
            if not _conflicting(text_a, toks_a, text_b, toks_b):
                continue
            pairs.append((idx_a, idx_b) if ts_a < ts_b else (idx_b, idx_a))
    return pairs
