"""Shared text normalization for the recall-governance layer.

Single home for the tokenization/normalization that every recall surface
(``/recall/v2``, ``/recall/active``, Hermes provider prefetch) needs to agree
on. Pure stdlib, no IO, no model load — safe to import eagerly from any
surface without circular-import or cost concerns.
"""

from __future__ import annotations

import re

# Segment Latin/digit runs separately from Hangul runs so a Korean particle
# glued onto a Latin proper noun ("OpenClaw랑", "Codex를") still yields the
# bare Latin token. Generic multilingual normalization (any name, not a
# hardcoded set), so token-based intent gates work for KO paraphrases.
_TOKEN_RE = re.compile(r"[a-z0-9]+|[가-힣]+")

# Slash/underscore separators ("current/status/Kanban", "current_status_kanban")
# normalize to whitespace so `\s+`-delimited patterns still match. Hyphens are
# left alone — they appear in legitimate tokens like "macos-calendar".
_SEPARATOR_RE = re.compile(r"[/_]+")


def tokenize(text: str) -> set[str]:
    """Script-boundary tokenizer: lowercased Latin/digit and Hangul runs of
    length > 1. Shared by every recall surface so intent gates are transitive."""
    return {tok for tok in _TOKEN_RE.findall((text or "").lower()) if len(tok) > 1}


def normalize_text(text: str) -> str:
    """Lowercase + strip. Cheap canonical form for substring feature checks."""
    return (text or "").strip().lower()


def normalize_separators(text: str) -> str:
    """Collapse slash/underscore separators to spaces (hyphens preserved)."""
    return _SEPARATOR_RE.sub(" ", text or "")


# Closed-class function words (EN determiners/pronouns/auxiliaries/conjunctions/
# prepositions/question words + common KO particles/auxiliaries). They carry no
# topical content, so recall surfaces compute DISTINCTIVE-token overlap with
# these removed — otherwise an ultra-common word like "do"/"the"/"is" counts as
# a topical match (an out-of-domain recipe prompt "overlapping" an identity row
# only on "do"). A closed linguistic class, never topic markers.
FUNCTION_WORD_STOPWORDS = frozenset(
    {
        # EN
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "if",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "for",
        "with",
        "from",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "am",
        "do",
        "does",
        "did",
        "doing",
        "have",
        "has",
        "had",
        "having",
        "can",
        "could",
        "should",
        "would",
        "will",
        "shall",
        "may",
        "might",
        "must",
        "i",
        "me",
        "my",
        "mine",
        "we",
        "us",
        "our",
        "ours",
        "you",
        "your",
        "yours",
        "he",
        "she",
        "it",
        "its",
        "they",
        "them",
        "their",
        "this",
        "that",
        "these",
        "those",
        "there",
        "here",
        "what",
        "which",
        "who",
        "whom",
        "whose",
        "when",
        "where",
        "why",
        "how",
        "not",
        "no",
        "yes",
        "so",
        "than",
        "then",
        "too",
        "also",
        "just",
        "very",
        "please",
        "about",
        "into",
        "over",
        "out",
        "up",
        "down",
        # KO common particles / auxiliaries / interrogatives (also appear glued)
        "있어",
        "없어",
        "뭐",
        "무엇",
        "무슨",
        "어떤",
        "어떻게",
        "왜",
        "언제",
        "어디",
        "그",
        "이",
        "저",
        "좀",
        "그냥",
    }
)


def content_tokens(text: str) -> set[str]:
    """tokenize() minus closed-class function words — distinctive content only.

    Shared by recall surfaces for topical-overlap decisions so a bare function
    word never reads as a topical match."""
    return tokenize(text) - FUNCTION_WORD_STOPWORDS


# ── Negation-scope detection (route-keyword arbitration) ───────────────────
# A route keyword that appears inside an explicit TOPIC-EXCLUSION negation ("not
# about codex", "코덱스 말고") is NOT positive route evidence — it is keyword
# residue the user has explicitly excluded. These detectors are a closed
# linguistic class (verbal EN negation incl. n't contractions; KO trailing
# negation markers — Korean negates AFTER the noun), never per-route phrases.
#
# Deliberately EXCLUDED: bare "no"/"without". Those are CONSTRAINT phrasing, not
# topic exclusion — the cost/budget/quality routes are DEFINED by "no paid API",
# "no local model", "no regression", and treating them as negation would stop
# exactly the prompts those routes must catch. Verbal negation ("this is not
# codex", "isn't about codex") is unambiguous topic exclusion and is kept.
_EN_NEGATION_CUES = frozenset(
    {
        "not",
        "never",
        "nor",
        "isnt",
        "arent",
        "wasnt",
        "werent",
        "dont",
        "doesnt",
        "didnt",
        "cant",
        "cannot",
        "wont",
        "couldnt",
        "wouldnt",
        "shouldnt",
        "aint",
    }
)
_KO_NEGATION_MARKERS = ("아니", "아닌", "아냐", "말고")
# Small windows: negation binds tightly to its target, so scanning only the few
# adjacent tokens avoids firing on an unrelated "not" several words away
# ("not sure … should I use codex" must NOT count codex as negated).
_NEGATION_PRECEDING_WINDOW = 3
_NEGATION_FOLLOWING_WINDOW = 3


def occurrence_is_negated(lowered: str, start: int, end: int) -> bool:
    """True when the token occupying ``lowered[start:end]`` sits inside an explicit
    negation scope: an EN negation cue within the few preceding tokens
    (apostrophes stripped so ``isn't``/``don't`` read as cues), or a KO negation
    marker within the few following tokens (Korean negates after the noun)."""
    before = lowered[:start].replace("\u2019", "").replace("'", "")
    preceding = _TOKEN_RE.findall(before)[-_NEGATION_PRECEDING_WINDOW:]
    for tok in preceding:
        if tok in _EN_NEGATION_CUES or any(m in tok for m in _KO_NEGATION_MARKERS):
            return True
    following = _TOKEN_RE.findall(lowered[end:])[:_NEGATION_FOLLOWING_WINDOW]
    return any(any(m in tok for m in _KO_NEGATION_MARKERS) for tok in following)


# ── Korean particle (josa) stripping ─────────��───────────────────────────
# Stripped to expose the stem so glued tokens like 크리스가 / 파타고니아에서 /
# 코스는 normalize to the same stem the corpus stores space-separated.
# Longest-match; only applied when the stem keeps >= 2 Hangul syllables, so
# short nouns that merely END in a particle syllable (e.g. 회의) are never
# butchered. No morphological analyzer dependency.
_KOREAN_PARTICLES = (
    # 3-syllable
    "에서는",
    "에게서",
    "에서도",
    "으로는",
    # 2-syllable
    "에서",
    "에게",
    "에는",
    "으로",
    "이나",
    "까지",
    "부터",
    "한테",
    "이랑",
    "라고",
    "처럼",
    "보다",
    "마다",
    "조차",
    "마저",
    "밖에",
    "께서",
    # 1-syllable
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "와",
    "과",
    "의",
    "도",
    "에",
    "로",
    "만",
    "랑",
)


def strip_korean_particle(token: str) -> str:
    """Return ``token`` with one trailing Korean particle removed when the
    remaining stem keeps >= 2 Hangul syllables; otherwise return it unchanged.
    Latin/digit tokens are returned unchanged."""
    if not token or not ("가" <= token[0] <= "힣"):
        return token
    for suffix in _KOREAN_PARTICLES:  # longest-first
        if len(token) > len(suffix) and token.endswith(suffix):
            stem = token[: -len(suffix)]
            if len(stem) >= 2:
                return stem
            break
    return token


def normalize_tokens(text: str) -> set[str]:
    """tokenize() augmented with Korean particle-stripped stems so glued Korean
    tokens match the space-separated stems the corpus stores. Augmentation, not
    replacement: every original token is preserved so existing full-token intent
    gates are unchanged; only Hangul tokens carrying a recognized particle gain a
    stem variant."""
    toks = tokenize(text)
    return {t for t in (toks | {strip_korean_particle(t) for t in toks}) if len(t) > 1}


def has_unnegated_match(pattern: re.Pattern[str], lowered: str) -> bool:
    """True when ``pattern`` matches ``lowered`` at least once OUTSIDE a negation
    scope (see :func:`occurrence_is_negated`). Route-keyword matchers use this so a
    negated keyword ("not about codex") is never counted as positive route
    evidence, while a later non-negated mention still matches."""
    return any(not occurrence_is_negated(lowered, m.start(), m.end()) for m in pattern.finditer(lowered))
