"""brain_core/late_interaction.py — phrase-level late-interaction rerank (M8.6).

Late-interaction reranking (ColBERT, ColBERTv2) computes maxsim aggregation
between query token embeddings and document token embeddings — fundamentally
different from cross-encoder rerank which scores [query, doc] as a single pair.
The advantage: long-form docs where the relevant span is ~5% of the text
benefit massively, because the maxsim is dominated by the matching span and
not diluted by the rest.

Pure ColBERTv2 needs a token-level embedder (each token gets its own vector).
The brain currently uses multilingual-e5-large-instruct which is sentence-level
(mean-pooled). Strict ColBERT isn't available without installing pylate +
downloading a ColBERTv2 model — and pylate has a Python 3.14 dependency
conflict (voyager) we can't resolve right now.

This module ships PHRASE-LEVEL late interaction as a working alternative:
  1. Split the query into 3-5 sub-phrases (sentence/clause split)
  2. Split each candidate doc into ~6-10 chunks (already exists post-WS2 semantic chunking)
  3. Embed each subphrase + each chunk via the existing Ollama embedder
  4. For each subphrase, find the max cosine similarity across the doc's chunks
  5. Sum the maxsims — that's the doc's late-interaction score
  6. Re-rank top-k candidates by this score

This captures the "long doc with one relevant span" win without needing a
new model. It costs N+M extra embed calls per query (where N=sub-phrases,
M=chunks) — typically 5+50=55 calls. With the Ollama batch endpoint, that's
~80-150ms.

Pluggable backend pattern:
  BRAIN_RERANK_BACKEND=cross_encoder   (default, uses BGE-reranker)
  BRAIN_RERANK_BACKEND=late_interaction (this module)
  BRAIN_RERANK_BACKEND=colbert         (future: pylate plug-in, currently no-op fallback)

Default OFF — search_unified.py keeps using cross_encoder_rerank unless the
env var is set. Safe rollout.

NOTE: BRAIN_RERANK_BACKEND is read at module import time (line ~50), so
flipping the env var at runtime has no effect until the brain server restarts.
Ops procedure: edit the LaunchAgent plist and `launchctl kickstart -k`.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

log = logging.getLogger("brain.late_interaction")

BACKEND = os.environ.get("BRAIN_RERANK_BACKEND", "cross_encoder").lower()

# ── Phrase splitter — same intuition as semantic_chunk but smaller granularity ──
_PHRASE_RE = re.compile(r"[^.!?。!?…\n,;]+(?:[.!?。!?…,;]+|$)", re.UNICODE)
MAX_QUERY_PHRASES = 5
MIN_PHRASE_CHARS = 10


def _split_query_phrases(query: str) -> list[str]:
    """Split a query into 1-5 sub-phrases for late-interaction maxsim."""
    if not query or not query.strip():
        return []
    phrases = [m.group(0).strip() for m in _PHRASE_RE.finditer(query)]
    phrases = [p for p in phrases if len(p) >= MIN_PHRASE_CHARS]
    if not phrases:
        return [query.strip()]
    return phrases[:MAX_QUERY_PHRASES]


def _embed(text: str) -> list[float] | None:
    try:
        from indexer import get_embedding

        return get_embedding(text[:1000], prefix="query")
    except Exception as exc:
        log.warning("late_interaction embed failed: %s", exc)
        return None


def _embed_doc(text: str) -> list[float] | None:
    try:
        from indexer import get_embedding

        return get_embedding(text[:1000], prefix="passage")
    except Exception as exc:
        log.warning("late_interaction doc embed failed: %s", exc)
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def maxsim_score(query: str, doc_text: str, *, doc_chunks: int = 6) -> float:
    """Compute maxsim late-interaction score for one (query, doc) pair.

    Splits query into phrases + doc into chunks, embeds both, sums max cosine
    similarities. Returns a normalized score in [0, 1] (clamped) so callers
    can blend it with other 0-1 scores.
    """
    phrases = _split_query_phrases(query)
    if not phrases:
        return 0.0

    # Split doc into ~doc_chunks chunks of equal length (defensive — semantic
    # chunking would be ideal but adds latency we don't want on hot path)
    if not doc_text or not doc_text.strip():
        return 0.0

    chunk_len = max(150, len(doc_text) // doc_chunks)
    chunks = [doc_text[i : i + chunk_len] for i in range(0, len(doc_text), chunk_len)]
    chunks = chunks[:doc_chunks]

    phrase_embs = [_embed(p) for p in phrases]
    chunk_embs = [_embed_doc(c) for c in chunks]

    valid_phrases = [e for e in phrase_embs if e]
    valid_chunks = [e for e in chunk_embs if e]
    if not valid_phrases or not valid_chunks:
        return 0.0

    total_maxsim = 0.0
    for p_emb in valid_phrases:
        max_sim = max(_cosine(p_emb, c_emb) for c_emb in valid_chunks)
        total_maxsim += max_sim

    return min(1.0, total_maxsim / len(valid_phrases))


def rerank(query: str, results: list[dict], top_k: int = 20) -> list[dict]:
    """Rerank a list of search results by late-interaction maxsim score.

    Mutates each result with `late_interaction_score`. Sorts by that score
    descending. Only reranks the top_k by current score (cost guard).
    """
    if BACKEND != "late_interaction":
        return results
    if not results:
        return results

    scored = sorted(results, key=lambda r: float(r.get("score", 0) or 0), reverse=True)
    head = scored[:top_k]
    tail = scored[top_k:]

    for r in head:
        text = r.get("content") or r.get("title") or ""
        try:
            maxsim = maxsim_score(query, text)
        except Exception as exc:
            log.warning("late_interaction rerank failed for one result: %s", exc)
            maxsim = 0.0
        r["late_interaction_score"] = round(maxsim, 4)
        # Blend: 60% original (RRF/CE), 40% maxsim — tunable
        old_score = float(r.get("score", 0) or 0)
        r["score"] = max(0.0, min(100.0, 0.6 * old_score + 0.4 * maxsim * 100))

    head.sort(key=lambda r: r.get("score", 0), reverse=True)
    return head + tail


def stats() -> dict:
    return {
        "backend": BACKEND,
        "available_backends": ["cross_encoder", "late_interaction", "colbert (future, needs pylate)"],
        "max_query_phrases": MAX_QUERY_PHRASES,
        "min_phrase_chars": MIN_PHRASE_CHARS,
    }
