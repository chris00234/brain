"""Recall result quality helpers.

Pure ranking/deduplication helpers used by ``routes.recall``. They live in the
recall-governance package because they classify result quality, not HTTP route
behavior.
"""

from __future__ import annotations

import re

from .source_authority import _TRUTH_CATEGORIES, result_category, result_metadata, result_text


def normalize_recall_signature(text: str) -> str:
    lowered = (text or "").lower()
    lowered = re.sub(r"https?://\S+", " ", lowered)
    lowered = re.sub(r"\b20\d{2}(?:[-_/]?w?\d{1,2})?(?:[-_/]\d{1,2})?\b", " ", lowered)
    lowered = re.sub(r"\b\d+(?:\.\d+)?%?\b", " ", lowered)
    tokens = [tok for tok in re.findall(r"[a-z0-9가-힣]+", lowered) if len(tok) > 2]
    stop = {
        "chris",
        "wants",
        "want",
        "prefers",
        "preference",
        "should",
        "that",
        "with",
        "from",
        "into",
        "the",
        "and",
        "for",
        "his",
        "her",
    }
    return " ".join(tok for tok in tokens if tok not in stop)


def near_duplicate_key(result: dict) -> str:
    text = result_text(result)
    sig = normalize_recall_signature(text)
    tokens = set(sig.split())
    # Known high-value Brain-quality preference appears in several learned/canonical
    # phrasings. Collapse it semantically so prefetch does not repeat it 3x.
    if {"brain", "eval", "score"}.issubset(tokens) and ({"improvement", "improvements"} & tokens):
        return "brain-eval-score-improvement-preference"
    if {"브레인", "평가"}.issubset(tokens) and ({"점수", "개선"} & tokens):
        return "brain-eval-score-improvement-preference"
    return sig


def is_near_duplicate_signature(candidate: str, kept: list[str]) -> bool:
    if not candidate:
        return False
    c_tokens = set(candidate.split())
    if len(c_tokens) < 4:
        return candidate in kept
    for existing in kept:
        if candidate == existing:
            return True
        e_tokens = set(existing.split())
        if len(e_tokens) < 4:
            continue
        overlap = len(c_tokens & e_tokens) / max(1, min(len(c_tokens), len(e_tokens)))
        if overlap >= 0.86:
            return True
    return False


def quality_rank_tuple(result: dict) -> tuple[float, float]:
    collection = str(result.get("collection") or "").lower()
    category = result_category(result)
    meta = result_metadata(result)
    review_state = str(meta.get("review_state") or result.get("review_state") or "").lower()
    durable = 0.0
    if collection == "canonical" and review_state in {"accepted", "approved", "canonical"}:
        durable += 3.0
    if collection in {"canonical", "distilled"}:
        durable += 1.0
    if category in _TRUTH_CATEGORIES:
        durable += 2.0
    try:
        score = float(result.get("score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    return durable, score


# Raw conversation / session-turn capture shape: a row whose text is a dialogue
# transcript (role-prefixed 'User:'/'Assistant:' turns). These are ingested
# session turns (and validation transcripts that merely QUOTE a probe), not
# curated answer atoms. Format/provenance signal, not a topic marker — mirror of
# the Hermes provider's same-named gate so both surfaces agree.
_CONVERSATION_TURN_RE = re.compile(r"(?im)(?:^|\n)\s*(?:user|assistant|human|유저|사용자|어시스턴트)\s*:")


def is_conversation_transcript_row(result: dict) -> bool:
    hay = "\n".join(str(result.get(k) or "") for k in ("title", "content"))
    if _CONVERSATION_TURN_RE.search(hay):
        return True
    low = hay.lower()
    return "user:" in low and "assistant:" in low
