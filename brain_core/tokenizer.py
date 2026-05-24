"""Shared tokenizer for reranking, search, and pipeline modules.

Improvements over the previous per-module `[a-z0-9_-]{3,}` regex:
  - Min token length 2 (catches "AI", "DB", "OS", etc.)
  - Korean character support (가-힣)
  - English + Korean stopword removal
"""

from __future__ import annotations

import re

_LATIN_RE = re.compile(r"[a-z0-9_\-]{2,}")
_KOREAN_RE = re.compile(r"[가-힣]{2,}")
_STOPWORDS = frozenset(
    {
        # English function words (articles, copula, modals, demonstratives,
        # interrogatives, common prepositions). Single-character English
        # words are already excluded by the {2,} length floor.
        "the",
        "is",
        "an",
        "in",
        "on",
        "of",
        "for",
        "to",
        "and",
        "or",
        "not",
        "it",
        "this",
        "that",
        "with",
        "from",
        "by",
        "at",
        "as",
        "be",
        "was",
        "are",
        "were",
        "been",
        "has",
        "had",
        "have",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "shall",
        "can",
        "what",
        "who",
        "whom",
        "whose",
        "where",
        "when",
        "why",
        "how",
        "which",
        "about",
        # Korean function words. Single-syllable particles like 은/는/이/가/을/를/의/에
        # don't pass the [가-힣]{2,} regex and are already excluded. This list
        # only filters multi-syllable function words that carry no domain
        # signal — conjunctions, demonstratives, interrogatives, formal
        # sentence endings, postposition compounds. Nouns/verbs/adjectives
        # with semantic content are intentionally NOT included.
        "그리고",  # and
        "그러나",  # but
        "그래서",  # so
        "또한",  # also
        "하지만",  # however
        "또는",  # or
        "혹은",  # or (alternative)
        "이것",  # this
        "그것",  # that
        "저것",  # that (over there)
        "이런",  # this kind of
        "그런",  # that kind of
        "저런",  # that kind of (over there)
        "무엇",  # what
        "어떻게",  # how
        "어디서",  # where (from)
        "언제",  # when
        "어디",  # where
        "누구",  # who
        "입니다",  # (formal copula ending)
        "됩니다",  # (formal "becomes" ending)
        "합니다",  # (formal "do" ending)
        "위해",  # for (the sake of)
        "통해",  # through
        "대해",  # about
    }
)


def tokenize(text: str) -> set[str]:
    lower = (text or "").lower()
    tokens = set(_LATIN_RE.findall(lower)) | set(_KOREAN_RE.findall(text or ""))
    return tokens - _STOPWORDS


# Exact-alias extractors. These preserve CASE and punctuation so a downstream
# rerank/bridge layer can detect verbatim matches for code-ish identifiers
# (env vars, account handles, paths) and personal names (Hangul, PascalCase).
# The general `tokenize()` above is lowercased + jaccard-friendly; this helper
# is the orthogonal "did the literal token appear?" check.
_PATH_RE = re.compile(r"/[A-Za-z0-9_.\-~]+(?:/[A-Za-z0-9_.\-~]+)+/?")
_ENV_VAR_RE = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")
_HANDLE_RE = re.compile(r"\b[a-z]+\d+\b")
_PASCAL_RE = re.compile(r"\b[A-Z][a-z]{2,}\b")
_HANGUL_RE = re.compile(r"[가-힣]{2,}")
# Words inside a Hangul token that are too generic to be aliases by themselves.
_PASCAL_STOPWORDS = frozenset(
    {
        "The",
        "This",
        "That",
        "When",
        "Where",
        "What",
        "From",
        "With",
    }
)


def extract_exact_aliases(text: str) -> list[str]:
    """Return verbatim alias tokens worth pinning during recall/store debug.

    Buckets:
      - absolute / relative file paths (``/Users/chrischo/.codex``)
      - SCREAMING_SNAKE_CASE env vars (``CODEX_HOME``, ``PATH``)
      - account-style handles (``claude1``, ``claude4``)
      - PascalCase / Proper nouns (``Daehyun``, ``Cho``)
      - Hangul (Korean) ≥ 2 syllables (``조대현``)

    Ordering is insertion-order across the buckets above (paths first since
    they are the most specific). Duplicates are dropped while preserving
    first-seen order so callers can build short, intent-anchored queries.
    """
    if not text:
        return []
    seen: dict[str, None] = {}

    def _add(value: str) -> None:
        if value and value not in seen:
            seen[value] = None

    for m in _PATH_RE.findall(text):
        _add(m)
    for m in _ENV_VAR_RE.findall(text):
        _add(m)
    for m in _HANDLE_RE.findall(text):
        _add(m)
    for m in _PASCAL_RE.findall(text):
        if m not in _PASCAL_STOPWORDS:
            _add(m)
    for m in _HANGUL_RE.findall(text):
        _add(m)

    return list(seen.keys())
