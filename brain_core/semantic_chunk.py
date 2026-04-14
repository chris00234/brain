"""brain_core/semantic_chunk.py — semantic-boundary chunking (M8.2).

Replaces character-window chunking with embedding-distance boundary detection.
The classic LangChain SemanticChunker / Greg Kamradt method: split text into
sentences, embed each sentence, compute distance between adjacent sentences,
mark boundaries at the top-N distance peaks, then group sentences into chunks.

Why this matters: character-based chunking (the existing `indexer.chunk_text`
default) frequently splits mid-sentence or mid-thought. Embedding a chunk that
ends mid-thought produces a vector that doesn't represent any coherent idea,
which hurts both retrieval (the query embedding won't match) and reranking (the
cross-encoder sees garbage context).

The cost is N sentence embeddings per document instead of N/M chunk embeddings
(where M is sentences per chunk). For a 1000-char chunk with ~5 sentences,
this is 5x the embedding calls. Acceptable for offline ingest (PDFs, notes),
NOT for hot-path queries.

Parent-child mode:
  When `parent_size_chars` is set, this also returns parent chunks — larger
  chunks (1024-2048 chars) that wrap multiple semantic chunks. Children are
  what we INDEX (small, retrieval-precise). Parents are what we RETURN at
  recall time (larger, more context for the LLM). Each child carries
  `parent_id` so search_unified can swap it in when needed.

Default OFF via BRAIN_SEMANTIC_CHUNKING env var. When enabled, ingest/pdfs.py
and ingest/personal.py use this module instead of indexer.chunk_text.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

log = logging.getLogger("brain.semantic_chunk")

ENABLED = os.environ.get("BRAIN_SEMANTIC_CHUNKING", "").lower() in {"1", "true", "yes"}

# Sentence boundary regex — handles English + Korean + Chinese + Japanese.
# Captures the sentence + its terminating punctuation.
_SENTENCE_RE = re.compile(
    r"[^.!?。!?…\n]+(?:[.!?。!?…]+|$)",
    re.UNICODE | re.MULTILINE,
)

# Default config — tuned for the brain's typical doc shape (PDFs, notes, sessions)
DEFAULT_TARGET_CHUNK_SIZE = 600  # chars — sweet spot for cross-encoder context
DEFAULT_MAX_CHUNK_SIZE = 1200  # hard cap — beyond this, force-break
DEFAULT_MIN_CHUNK_SIZE = 200  # don't emit fragments smaller than this
DEFAULT_PARENT_SIZE = 1800  # parent chunks for context
DEFAULT_PERCENTILE_THRESHOLD = 90  # boundary at sentences with top-10% pairwise distance


def _split_sentences(text: str) -> list[str]:
    """Sentence-level split. Falls back to newline-split for code/markdown."""
    if not text or not text.strip():
        return []

    # If the text has lots of newlines (markdown/code), prefer line-level
    if text.count("\n") > len(text) // 80:
        return [line.strip() for line in text.split("\n") if line.strip()]

    sentences = [m.group(0).strip() for m in _SENTENCE_RE.finditer(text)]
    return [s for s in sentences if s]


def _embed_sentence(text: str) -> list[float] | None:
    """Embed via local Ollama. Returns None on failure."""
    try:
        from indexer import get_embedding

        return get_embedding(text[:1000], prefix="passage")
    except Exception as exc:
        log.warning("semantic_chunk embed failed: %s", exc)
        return None


def _cosine_distance(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 1.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 1.0
    sim = dot / (na * nb)
    return max(0.0, 1.0 - sim)


def _find_boundaries(distances: list[float], percentile: float = 90.0) -> list[int]:
    """Return sentence indices where a chunk boundary should land.

    Uses the percentile of adjacent-sentence distances as the threshold.
    distances[i] is the distance between sentence i and i+1, so a boundary
    at index i means "split after sentence i."
    """
    if not distances:
        return []
    sorted_d = sorted(distances, reverse=True)
    cutoff_idx = max(1, int(len(sorted_d) * (100 - percentile) / 100))
    threshold = sorted_d[cutoff_idx - 1] if cutoff_idx <= len(sorted_d) else sorted_d[-1]
    return [i for i, d in enumerate(distances) if d >= threshold]


def chunk_semantic(
    text: str,
    *,
    target_size: int = DEFAULT_TARGET_CHUNK_SIZE,
    max_size: int = DEFAULT_MAX_CHUNK_SIZE,
    min_size: int = DEFAULT_MIN_CHUNK_SIZE,
    percentile: float = DEFAULT_PERCENTILE_THRESHOLD,
    parent_size: int | None = None,
) -> list[dict]:
    """Split text into semantic chunks.

    Returns a list of dicts:
        {"content": str, "section": str, "chunk_id": str, "parent_id": str | None}

    If parent_size is set, parent atoms are emitted with a leading
    {"content": str, "is_parent": True, "chunk_id": str, "child_ids": [str, ...]}
    entry per parent group. Children point at their parent via parent_id.

    When the embedder fails or the text is too short for boundary detection,
    falls back to character-window chunking (defensive — never crashes ingest).
    """
    if not text or len(text.strip()) < min_size:
        return [{"content": text, "section": "full", "chunk_id": uuid.uuid4().hex[:12], "parent_id": None}]

    sentences = _split_sentences(text)
    if len(sentences) <= 2:
        return [{"content": text, "section": "full", "chunk_id": uuid.uuid4().hex[:12], "parent_id": None}]

    # Embed each sentence
    embeddings: list[list[float] | None] = []
    for s in sentences:
        embeddings.append(_embed_sentence(s))

    # If too many embed failures, fall back to character-window chunking
    failed = sum(1 for e in embeddings if e is None)
    if failed > len(sentences) // 2:
        log.warning("semantic_chunk: too many embed failures (%d/%d) — falling back", failed, len(sentences))
        from indexer import chunk_text

        raw = chunk_text(text, max_size=max_size)
        out = []
        for raw_chunk in raw:
            content_str = raw_chunk["content"] if isinstance(raw_chunk, dict) else str(raw_chunk)
            out.append(
                {
                    "content": content_str,
                    "section": "fallback",
                    "chunk_id": uuid.uuid4().hex[:12],
                    "parent_id": None,
                }
            )
        return out

    # Compute adjacent-sentence distances
    distances: list[float] = []
    for i in range(len(sentences) - 1):
        a = embeddings[i]
        b = embeddings[i + 1]
        if a is None or b is None:
            distances.append(1.0)  # treat missing embeds as a forced boundary
        else:
            distances.append(_cosine_distance(a, b))

    boundaries = set(_find_boundaries(distances, percentile=percentile))

    # Group sentences into chunks at each boundary
    chunks_text: list[str] = []
    buf: list[str] = []
    for i, sent in enumerate(sentences):
        buf.append(sent)
        buf_len = sum(len(s) + 1 for s in buf)
        # End chunk on any of:
        #   - we hit a semantic boundary AND have minimum content
        #   - we reached target_size
        #   - we hit max_size (force-break)
        is_boundary = i in boundaries
        if (is_boundary and buf_len >= min_size) or buf_len >= target_size or buf_len >= max_size:
            chunks_text.append(" ".join(buf).strip())
            buf = []
    if buf:
        last = " ".join(buf).strip()
        if last:
            chunks_text.append(last)

    # Merge any chunk smaller than min_size into its predecessor
    merged: list[str] = []
    for chunk_str in chunks_text:
        if merged and len(chunk_str) < min_size:
            merged[-1] = (merged[-1] + " " + chunk_str).strip()
        else:
            merged.append(chunk_str)

    # Emit child + (optional) parent records
    out: list[dict] = []

    if parent_size:
        # Group consecutive children into parents whose total size ≤ parent_size
        parent_buf_children: list[dict] = []
        parent_buf_size = 0
        parent_id = uuid.uuid4().hex[:12]

        for chunk_str in merged:
            child_id = uuid.uuid4().hex[:12]
            child = {
                "content": chunk_str,
                "section": "semantic",
                "chunk_id": child_id,
                "parent_id": parent_id,
            }
            if parent_buf_size + len(chunk_str) > parent_size and parent_buf_children:
                # Emit parent first, then start a new parent
                out.append(
                    {
                        "content": " ".join(c["content"] for c in parent_buf_children),
                        "section": "parent",
                        "chunk_id": parent_id,
                        "parent_id": None,
                        "is_parent": True,
                        "child_ids": [c["chunk_id"] for c in parent_buf_children],
                    }
                )
                out.extend(parent_buf_children)
                parent_id = uuid.uuid4().hex[:12]
                child["parent_id"] = parent_id
                parent_buf_children = []
                parent_buf_size = 0
            parent_buf_children.append(child)
            parent_buf_size += len(chunk_str)

        # Emit final parent
        if parent_buf_children:
            out.append(
                {
                    "content": " ".join(c["content"] for c in parent_buf_children),
                    "section": "parent",
                    "chunk_id": parent_id,
                    "parent_id": None,
                    "is_parent": True,
                    "child_ids": [c["chunk_id"] for c in parent_buf_children],
                }
            )
            out.extend(parent_buf_children)
    else:
        for chunk_str in merged:
            out.append(
                {
                    "content": chunk_str,
                    "section": "semantic",
                    "chunk_id": uuid.uuid4().hex[:12],
                    "parent_id": None,
                }
            )

    return out


def chunk_with_fallback(text: str, max_size: int = DEFAULT_MAX_CHUNK_SIZE) -> list[dict]:
    """Convenience wrapper: semantic chunking when ENABLED, character chunking otherwise.

    Drop-in replacement for indexer.chunk_text from ingest call sites.
    """
    if ENABLED:
        return chunk_semantic(text, max_size=max_size)
    from indexer import chunk_text

    return chunk_text(text, max_size=max_size)


def stats() -> dict:
    return {
        "enabled": ENABLED,
        "target_size": DEFAULT_TARGET_CHUNK_SIZE,
        "max_size": DEFAULT_MAX_CHUNK_SIZE,
        "min_size": DEFAULT_MIN_CHUNK_SIZE,
        "percentile": DEFAULT_PERCENTILE_THRESHOLD,
        "parent_size": DEFAULT_PARENT_SIZE,
    }
