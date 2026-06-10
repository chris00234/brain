"""brain_core/ingest_classifier.py — Layer A LLM classifier for Brain Hygiene.

Called from /memory POST and atoms_store.upsert_atom BEFORE the write to
extract three structured fields used by the retrieval filter:

  topic_key         — e.g. "preference:frontend_framework", "fact:hardware:primary"
                      Same topic_key ≈ same subject; used for supersession.
  speaker_entity    — 'chris' | 'quoted:<name>' | 'agent:<name>' | 'test'
                      Chris's direct statements vs quoted vs agent inferences.
  scope             — 'global' | 'project' | 'session'
                      Narrow-scope memories are excluded from global retrieval.

One Sage LLM call per ingest. Cached by content hash so re-ingests are
NOOP on the classifier path. If Sage is unreachable, falls back to
heuristics (provisional=True, speaker='chris' if author_agent=='claude'
else 'agent:<author>').

Wired into: server.py::create_memory (Layer A gate).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

try:
    from cli_llm import dispatch as _dispatch
except ImportError:
    _dispatch = None  # type: ignore[assignment]

log = logging.getLogger("brain.ingest_classifier")

# Heuristics for speaker classification — used as fallback AND as input
# context for the LLM prompt so it doesn't have to guess.
FIRST_PERSON_PATTERNS = [
    re.compile(r"\b(?:I|my|me|myself|mine)\b", re.IGNORECASE),
    re.compile(r"(?:^|\s)(?:내|나는|나의|내가|내게)", re.IGNORECASE),
    re.compile(r"\bchris\s+(?:prefers|wants|decided|uses|likes|chose)", re.IGNORECASE),
]
QUOTED_PATTERNS = [
    re.compile(r'"[^"]+"\s*(?:said|says|mentioned)', re.IGNORECASE),
    re.compile(r"(?:according to|per)\s+\w+", re.IGNORECASE),
    re.compile(r"\b(?:wrote|replied|emailed)\s*:", re.IGNORECASE),
]
SESSION_SCOPE_PATTERNS = [
    re.compile(r"\b(?:in this session|in this chat|right now|for this turn)\b", re.IGNORECASE),
    re.compile(r"(?:이번|지금|이 세션|여기서만)", re.IGNORECASE),
]
PROJECT_SCOPE_PATTERNS = [
    re.compile(r"\b(?:for\s+(?:brain-ui|mcc|openclaw|this project))\b", re.IGNORECASE),
    re.compile(r"\b(?:in\s+(?:brain-ui|mcc|openclaw))\b", re.IGNORECASE),
]


@dataclass
class IngestClassification:
    topic_key: str | None
    speaker_entity: str
    scope: str
    provisional: bool
    confidence: float
    reason: str
    source: str  # "llm" | "heuristic" | "cache"


# ── Cache — content_hash → classification, 1h TTL ────────────────

_cache: dict[str, tuple[float, IngestClassification]] = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 3600.0
_CACHE_MAX = 256


def _content_hash(content: str, author_agent: str = "", category: str = "") -> str:
    """CR5 fix (2026-04-14): include author_agent + category in cache key.
    Previously the cache was content-only, so two different agents writing
    the same text got the same cached speaker_entity (the FIRST caller's
    classification). Cross-agent contamination leaked until the 1h TTL
    expired, breaking the speaker='chris' retrieval filter."""
    key = f"{category}:{author_agent}:{content}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _cache_get(key: str) -> IngestClassification | None:
    with _cache_lock:
        entry = _cache.get(key)
        if not entry:
            return None
        ts, classification = entry
        if time.time() - ts > _CACHE_TTL:
            del _cache[key]
            return None
        return classification


def _cache_put(key: str, classification: IngestClassification) -> None:
    with _cache_lock:
        _cache[key] = (time.time(), classification)
        if len(_cache) > _CACHE_MAX:
            oldest = min(_cache, key=lambda k: _cache[k][0])
            del _cache[oldest]


# ── Heuristic fallback (no LLM) ──────────────────────────────────


def _heuristic_classify(
    content: str,
    author_agent: str,
    category: str,
) -> IngestClassification:
    """Fast deterministic classification when LLM is unavailable.

    v3 code-review fix (2026-04-14): previously defaulted provisional=True
    regardless of signal, which silently hid backfilled atoms from retrieval.
    Now only marks provisional=True when there's a POSITIVE signal of
    uncertainty: quoted speaker, session scope, or agent inference WITHOUT
    first-person signal. Clean Chris-authored content → provisional=False.
    """
    # Speaker detection
    speaker = "chris"
    is_quoted = any(p.search(content) for p in QUOTED_PATTERNS)
    has_first_person = any(p.search(content) for p in FIRST_PERSON_PATTERNS)

    if is_quoted:
        speaker = "quoted:unknown"
    elif author_agent and author_agent not in ("claude", "chris", "user"):
        speaker = f"agent:{author_agent}"

    # Scope detection
    scope = "global"
    if any(p.search(content) for p in SESSION_SCOPE_PATTERNS):
        scope = "session"
    elif any(p.search(content) for p in PROJECT_SCOPE_PATTERNS):
        scope = "project"

    # Topic key — simple extraction from first few words + category
    words = re.findall(r"\w+", content[:200].lower())[:4]
    topic_key = f"{category}:{'_'.join(words)}"[:80] if words else None

    # Provisional ONLY when uncertain signal present:
    #   - quoted speaker (not Chris's own words)
    #   - session-scoped (not globally valid)
    #   - agent-inferred third-person without first-person anchor
    provisional = is_quoted or scope == "session" or (speaker.startswith("agent:") and not has_first_person)

    return IngestClassification(
        topic_key=topic_key,
        speaker_entity=speaker,
        scope=scope,
        provisional=provisional,
        confidence=0.7 if not provisional else 0.5,
        reason="heuristic" + ("_provisional" if provisional else "_trusted"),
        source="heuristic",
    )


# ── LLM classification (Sage) ────────────────────────────────────

_SAGE_PROMPT = """Classify this new memory entry for Chris's personal brain.

Return ONLY a JSON object with these fields:
  "topic_key": a short stable key like "preference:frontend_framework" or "fact:hardware:primary_machine". Same subject → same key. Max 60 chars. Always lowercase English snake_case, even for Korean or mixed-language content — translate the topic so the same subject in any language maps to the same key (한국어 메모도 영어 키로).
  "speaker_entity": one of "chris" (Chris's own statement), "quoted:<name>" (someone else quoted by Chris), "agent:<name>" (agent inference about Chris).
  "scope": "global" (always applies), "project:<name>" (one project only), "session" (this session only).
  "provisional": true/false — true if the claim is uncertain, needs reinforcement before acting on it.
  "confidence": 0.0-1.0 — how confident YOU are in this classification.
  "reason": brief (<60 chars) why.

Context:
  author_agent: {author_agent}
  category: {category}

Memory content:
{content}

Return JSON only, no prose."""


def _llm_classify(
    content: str,
    author_agent: str,
    category: str,
    timeout: int = 15,
    max_backends: int | None = None,
) -> IngestClassification | None:
    """Single Sage LLM call to classify. Returns None on any failure."""
    if _dispatch is None:
        return None
    try:
        prompt = _SAGE_PROMPT.format(
            author_agent=author_agent or "unknown",
            category=category or "fact",
            content=content[:1500],
        )
        # NOTE: main Sage session (isolation via session_id doesn't work
        # with arbitrary strings — needs valid UUID + OpenClaw config).
        result = _dispatch(
            agent="sage",
            message=prompt,
            thinking="low",
            timeout=timeout,
            max_backends=max_backends,
        )
        if not result.ok or not result.text:
            return None
        text = result.text.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        data = json.loads(text.strip())
        return IngestClassification(
            topic_key=str(data.get("topic_key") or "")[:80] or None,
            speaker_entity=str(data.get("speaker_entity") or "chris")[:40],
            scope=str(data.get("scope") or "global")[:40],
            provisional=bool(data.get("provisional", False)),
            confidence=float(data.get("confidence", 0.7)),
            reason=str(data.get("reason") or "")[:100],
            source="llm",
        )
    except Exception as e:
        log.debug("llm classify failed: %s", e)
        return None


# ── Public entry point ────────────────────────────────────────────


def classify(
    content: str,
    *,
    author_agent: str = "claude",
    category: str = "fact",
    use_llm: bool = True,
    force_llm: bool = False,
    timeout: int = 15,
    max_backends: int | None = None,
) -> IngestClassification:
    """Classify a memory at ingest time. Used by /memory POST Layer A gate.

    Returns an IngestClassification with topic_key/speaker_entity/scope/provisional
    so the caller can populate the new hygiene columns on atoms.

    Cached by content+agent+category hash (1h TTL) so re-ingests and NOOP
    dedupes don't re-spend LLM tokens. LLM failure falls through to
    heuristic mode.

    CR6 fix (2026-04-14): `force_llm=True` bypasses the cache AND requires
    an LLM result — returns None if LLM fails. Used by llm_backlog.classify
    handler so backlog retries aren't served stale heuristic results from
    the cache (previously the backlog drained would re-call classify(),
    hit the cached heuristic entry, and return False → retry counter →
    abandoned after 5 drains with no actual LLM upgrade ever attempted).
    """
    content_h = _content_hash(content, author_agent, category)
    if not force_llm:
        cached = _cache_get(content_h)
        if cached:
            return cached

    if use_llm:
        llm_result = _llm_classify(
            content,
            author_agent,
            category,
            timeout=timeout,
            max_backends=max_backends,
        )
        if llm_result:
            _cache_put(content_h, llm_result)
            return llm_result
        if force_llm:
            # Backlog retry path: caller wants LLM or nothing. Don't
            # fall through to heuristic — that would mark the entry
            # "done" with wrong data.
            return None  # type: ignore[return-value]
        # F1 + llm_backlog: LLM unavailable, enqueue for retry when quota
        # returns. The heuristic result below carries us through until then.
        # The enqueue happens from server.py::create_memory where the caller
        # has access to atom_id after upsert_atom — here we only have content.

    heuristic = _heuristic_classify(content, author_agent, category)
    _cache_put(content_h, heuristic)
    return heuristic
