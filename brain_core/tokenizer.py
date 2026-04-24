"""Shared tokenizer for reranking, search, and pipeline modules.

Improvements over the previous per-module `[a-z0-9_-]{3,}` regex:
  - Min token length 2 (catches "AI", "DB", "OS", etc.)
  - Korean character support (가-힣)
  - English stopword removal
"""

from __future__ import annotations

import re

_LATIN_RE = re.compile(r"[a-z0-9_\-]{2,}")
_KOREAN_RE = re.compile(r"[가-힣]{2,}")
_STOPWORDS = frozenset(
    {
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
    }
)


def tokenize(text: str) -> set[str]:
    lower = (text or "").lower()
    tokens = set(_LATIN_RE.findall(lower)) | set(_KOREAN_RE.findall(text or ""))
    return tokens - _STOPWORDS
