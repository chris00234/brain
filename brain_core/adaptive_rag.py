"""brain_core/adaptive_rag.py — query-complexity-aware retrieval routing (M8.4).

Adaptive-RAG (arXiv:2403.14403) classifies queries by complexity and routes
them to different retrieval depths:

  - SIMPLE   — atomic factual lookup ("what is X"). Skip CRAG / multi-hop.
  - SINGLE   — needs RAG but one hop is enough ("how does X work").
  - MULTI    — needs reasoning across multiple sources ("compare X and Y",
               "what changed between X and Z").

The brain already has `_classify_intent` for source weighting (graph vs RAG vs
canonical) and `_route_sources` for skipping irrelevant sources. This module
adds a different axis: how DEEP to retrieve.

Why this matters for M7-WS3:
The CRAG default-on flip was deferred because triggering on every query at
the current threshold ate ~1.5s/query (vs ~330ms baseline). With Adaptive-RAG,
CRAG only fires for MULTI-class queries, which are typically <20% of traffic.
That keeps the latency budget while still capturing the recall lift on hard
queries.

Wire-up:
  server.py:_recall_v2 → classify(q) → if SIMPLE, force iterative=False even
  when caller passed iterative=True; if MULTI, allow iterative even when not
  explicitly requested IF a future flag enables auto-CRAG.

Module-level kill switch via BRAIN_ADAPTIVE_RAG env var (default off until
measured).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

log = logging.getLogger("brain.adaptive_rag")

ENABLED = os.environ.get("BRAIN_ADAPTIVE_RAG", "").lower() in {"1", "true", "yes"}


# ── Heuristic patterns for query complexity ──────────────────────────────────
# These are deliberately CHEAP regex patterns. The Adaptive-RAG paper uses a
# small fine-tuned classifier (~50M params); we use heuristics + the existing
# _classify_intent signal. Good enough for >80% accuracy in our setting.

# SIMPLE: atomic factual ("what is X", "who is X", short wh-questions)
_SIMPLE_PATTERNS = re.compile(
    r"^\s*(what(?:'s| is)?|who(?:'s| is)?|when(?:'s| is)?|where(?:'s| is)?|"
    r"무엇|뭐(?:야|지)?|언제|어디(?:야|지)?|누구(?:야|지)?|얼마)\b",
    re.IGNORECASE,
)

# MULTI: comparison, reasoning, multi-fact synthesis. Tightened post-M8
# rollout: dropped `왜` (too common in Korean factual questions) and `차이`/
# `변화`/`비교` (Python `\b` doesn't word-boundary Korean syllable blocks, so
# these false-fired on substrings like `차이점`/`비교적`/`변화에`). Kept the
# explicit comparison lexicon and the multi-character "차이점은"/"장단점" forms
# that are unambiguous comparison signals.
_MULTI_PATTERNS = re.compile(
    r"\b(compare|contrast|differences?|both|either|neither|"
    r"vs|versus|trade.?off|pros and cons|why does|why is|"
    r"what changed|what's changed|how has|how have)\b|"
    r"(차이점은|장단점|어떻게 다른|뭐가 다른)",
    re.IGNORECASE,
)

# Strong MULTI signals: query contains 2+ entities or temporal connectives
_MULTI_TEMPORAL = re.compile(
    r"\b(before|after|during|since|until|then|now|currently|previously|"
    r"이전|이후|동안|부터|까지|지금|현재|과거)\b",
    re.IGNORECASE,
)

# Multi-clause indicators (commas, "and ... and", multi-question)
_MULTI_CLAUSE = re.compile(r"\?[^?]*\?|\band\b.+\band\b", re.IGNORECASE)


@dataclass
class QueryClass:
    label: str  # "simple" | "single" | "multi"
    confidence: float  # 0.0-1.0
    reasons: list[str]
    word_count: int


def classify(query: str) -> QueryClass:
    """Classify a query into simple / single / multi retrieval depth.

    The default class is "single" — meaning normal RAG with one fan-out hop.
    Promote to "multi" only on strong signals; demote to "simple" only on
    obvious atomic lookups.
    """
    if not query or not query.strip():
        return QueryClass(label="single", confidence=0.5, reasons=["empty"], word_count=0)

    q = query.strip()
    word_count = len(q.split())
    reasons: list[str] = []

    # Strong MULTI signals first — comparison, reasoning, multi-clause
    multi_score = 0
    if _MULTI_PATTERNS.search(q):
        multi_score += 2
        reasons.append("multi_pattern")
    if _MULTI_TEMPORAL.search(q):
        multi_score += 1
        reasons.append("temporal")
    if _MULTI_CLAUSE.search(q):
        multi_score += 1
        reasons.append("multi_clause")
    if word_count > 25:
        multi_score += 1
        reasons.append("long_query")

    # M8 follow-up: raised promotion threshold from 2 → 3. With the threshold
    # at 2, a single _MULTI_PATTERNS match alone (worth 2) flipped the class
    # — too many false positives on the Korean half of the extended eval
    # caused -5pt source_hit. At 3 we require a pattern AND a secondary signal
    # (temporal connective, multi-clause, or long query).
    if multi_score >= 3:
        return QueryClass(
            label="multi",
            confidence=min(1.0, 0.5 + 0.12 * multi_score),
            reasons=reasons,
            word_count=word_count,
        )

    # SIMPLE signals — atomic wh-question with short word count
    if _SIMPLE_PATTERNS.match(q) and word_count <= 12:
        reasons.append("atomic_wh_short")
        return QueryClass(
            label="simple",
            confidence=0.8,
            reasons=reasons,
            word_count=word_count,
        )

    # Default
    reasons.append("default_single")
    return QueryClass(
        label="single",
        confidence=0.6,
        reasons=reasons,
        word_count=word_count,
    )


def should_use_crag(query: str, caller_explicit: bool = False) -> tuple[bool, str]:
    """Decide whether CRAG iterative retrieval should fire for this query.

    `caller_explicit` is True when the caller passed `iterative=true` in the
    query params. We respect that EXCEPT for SIMPLE queries where CRAG is
    pure latency cost with zero recall benefit.

    Returns (use_crag, reason).
    """
    if not ENABLED:
        return caller_explicit, "adaptive_rag_disabled"

    classification = classify(query)

    if classification.label == "simple":
        if caller_explicit:
            return False, f"simple_query_overrides_explicit_{classification.confidence:.2f}"
        return False, "simple_query"

    if classification.label == "multi":
        return True, f"multi_query_{classification.confidence:.2f}"

    # single → only if caller asked
    return caller_explicit, "single_query_caller_choice"


def should_skip_atoms(query: str) -> tuple[bool, str]:
    """Decide whether atoms-tier filtering can be skipped (latency win).

    SIMPLE atomic factual queries don't benefit from supersession filtering
    because they typically hit canonical/chris/* which is already curated.
    """
    if not ENABLED:
        return False, "adaptive_rag_disabled"

    classification = classify(query)
    if classification.label == "simple":
        return True, "simple_query_skips_atoms"
    return False, classification.label


def stats() -> dict:
    return {
        "enabled": ENABLED,
        "classes": ["simple", "single", "multi"],
        "default_class": "single",
        "kill_switch": "BRAIN_ADAPTIVE_RAG",
    }
