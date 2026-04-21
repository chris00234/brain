"""brain_core/sparse_tokenizer.py — BM25 sparse-vector tokenizer.

Produces ``qdrant_client.SparseVector`` shaped tuples for hybrid search.
The tokenization mirrors SQLite FTS5's ``unicode61 remove_diacritics 2``
so BM25 ranking stays consistent with the legacy keyword fallback while
the Qdrant sparse index is being populated.

Qdrant scoring convention: we send raw term frequencies and let the
server compute BM25 via the ``Modifier.IDF`` flag on the sparse vector
config. That means this encoder does *not* compute IDF — just TF.

Index space: token → stable uint32 via MD5(text)[:4]. Hash collisions
are extremely rare on brain's ~100k-token vocabulary and cost nothing
semantically (collisions just slightly broaden match sets, never narrow).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

_WORD_RE = re.compile(r"\w+", re.UNICODE)

# Minimal English stopword list — purely an optimization so very common
# terms don't dominate the sparse vector. Intentionally small to stay
# language-agnostic. Korean has no equivalent here; multilingual stopword
# sets hurt recall more than they help at this corpus size.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "he",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "was",
        "were",
        "will",
        "with",
    }
)


def tokenize(text: str) -> list[str]:
    """Unicode word tokens, diacritics stripped, lowercased.

    Matches FTS5 unicode61 remove_diacritics=2 semantics so sparse-side
    BM25 and FTS5-side BM25 rank similar surface forms the same way.
    """
    if not text:
        return []
    normalized = unicodedata.normalize("NFD", text)
    stripped = "".join(c for c in normalized if unicodedata.category(c) != "Mn")
    return [t for t in (tok.lower() for tok in _WORD_RE.findall(stripped)) if t and t not in _STOPWORDS]


def token_index(token: str) -> int:
    """Deterministic uint32 index for a token."""
    digest = hashlib.md5(token.encode("utf-8"), usedforsecurity=False).digest()
    return int.from_bytes(digest[:4], "big")


def encode(text: str) -> tuple[list[int], list[float]]:
    """Return (indices, values) for a SparseVector.

    Values are raw term frequencies; Qdrant's ``Modifier.IDF`` converts to
    BM25 at query time.
    """
    if not text:
        return [], []
    tokens = tokenize(text)
    if not tokens:
        return [], []
    tf: dict[int, float] = {}
    for tok in tokens:
        idx = token_index(tok)
        tf[idx] = tf.get(idx, 0.0) + 1.0
    indices = sorted(tf.keys())
    values = [tf[i] for i in indices]
    return indices, values
