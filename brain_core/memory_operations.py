"""brain_core/memory_operations.py — Mem0-inspired operation semantics.

Classify incoming memories as ADD / UPDATE / DELETE / NOOP before storing.
Prevents memory accumulation without intent over long time horizons.
"""

from __future__ import annotations

import logging
import re
from typing import Literal

from vector_store import get_vector_store

log = logging.getLogger("brain.memory_operations")

Operation = Literal["ADD", "UPDATE", "DELETE", "NOOP"]

# Thresholds tuned from audit data
DUPLICATE_COSINE = 0.05  # near-exact match → NOOP
UPDATE_COSINE = 0.15  # semantic similarity → potential UPDATE
TOKEN_OVERLAP_MAX = 0.7  # below this + high semantic sim → refinement (UPDATE)
PREFERENCE_UPDATE_COSINE = 0.40  # preferences about similar topics (vs 0.15 default)

_WORD_RE = re.compile(r"[a-z0-9_\-]{3,}")

_PREF_VERB_RE = re.compile(
    r"\b(?:prefer|like|use|uses|using|switch(?:ed)?\s+to|chose|moved?\s+to|adopted|favor|favou?rs?)\s+(.+?)(?:\s+(?:for|over|instead|because|when|but|$))",
    re.IGNORECASE,
)


def _extract_preference_subject(text: str) -> set[str]:
    """Extract the object of preference verbs for topic matching."""
    subjects = set()
    for m in _PREF_VERB_RE.finditer(text):
        raw = m.group(1).strip().lower()
        subjects.update(w for w in _WORD_RE.findall(raw) if len(w) > 2)
    return subjects


def _tokenize(text: str) -> set[str]:
    return set(_WORD_RE.findall((text or "").lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    u = a | b
    return len(a & b) / len(u) if u else 0.0


def classify_operation(
    new_content: str,
    new_embedding: list[float],
    new_confidence: float,
    sem_col_id: str = "semantic_memory",  # legacy name kept for call-site compat
    category: str = "",
    chroma_url: str = "",  # unused; retained for signature compat
) -> tuple[Operation, str | None, dict]:
    """Classify a new memory against existing ones.

    Returns:
        (operation, superseded_id_if_update, diagnostics)

    Diagnostics dict contains: top_distance, top_overlap, top_confidence, reason

    The ``sem_col_id`` / ``chroma_url`` parameters are retained for
    backwards compatibility with existing callers (learn.py). Under the
    VectorStore abstraction we address by collection name directly, so
    these are ignored — the collection is always "semantic_memory".
    """
    del chroma_url  # unused under VectorStore
    is_pref = category == "preference"

    if not new_embedding:
        return ("ADD", None, {"reason": "no embedding available"})

    # Target collection name: any non-UUID string is treated as a name;
    # legacy UUID values from old callers fall back to the canonical name.
    collection = sem_col_id if sem_col_id and "-" not in sem_col_id else "semantic_memory"
    where_filter = {"category": "preference"} if is_pref else None
    k = 5 if is_pref else 3

    try:
        hits = get_vector_store().query(
            collection,
            vector=new_embedding,
            k=k,
            filter=where_filter,
            with_payload=True,
        )
    except Exception as e:
        log.debug("classify: query failed (%s) — defaulting to ADD", e)
        return ("ADD", None, {"reason": f"query failed: {e}"})

    if not hits:
        return ("ADD", None, {"reason": "no similar memories"})

    # Preserve the distance-based variables the rank loop expects.
    # ChromaStore hands back score = 1 - cosine_distance, so we invert
    # back to distance here instead of rewriting the rank logic.
    distances = [max(0.0, 1.0 - h.score) for h in hits]
    ids = [h.id for h in hits]
    docs = [h.document or "" for h in hits]
    metas = [h.payload or {} for h in hits]

    # Walk candidates in rank order, skipping memories that are already superseded
    # (they're chain heads that shouldn't compete with new memories).
    for rank in range(len(ids)):
        cand_dist = float(distances[rank]) if rank < len(distances) else 1.0
        cand_id = ids[rank]
        cand_doc = docs[rank] if rank < len(docs) else ""
        cand_meta = (metas[rank] if rank < len(metas) else {}) or {}
        try:
            cand_conf = float(cand_meta.get("confidence", 0.5))
        except (ValueError, TypeError):
            cand_conf = 0.5

        # Skip already-superseded candidates — find the actual current head of chain
        if cand_meta.get("superseded_by"):
            continue

        diagnostics = {
            "top_distance": round(cand_dist, 4),
            "top_id": cand_id,
            "top_confidence": cand_conf,
            "rank_used": rank,
        }

        # Exact duplicate — very close + same-or-higher existing confidence.
        # For preferences: structurally identical sentences with DIFFERENT subjects
        # ("prefers React" vs "prefers Vue") have near-zero cosine distance but
        # represent a preference CHANGE, not a duplicate. Check subject overlap.
        if cand_dist < DUPLICATE_COSINE and cand_conf >= new_confidence:
            if is_pref:
                new_subj = _extract_preference_subject(new_content)
                old_subj = _extract_preference_subject(cand_doc)
                if new_subj and old_subj and not (new_subj & old_subj):
                    # Subjects differ — this is a preference change, not a duplicate
                    return (
                        "UPDATE",
                        cand_id,
                        {**diagnostics, "reason": "preference subject changed"},
                    )
            return ("NOOP", None, {**diagnostics, "reason": "near-exact duplicate"})

        # Near-duplicate — may be refinement (UPDATE) or new territory (ADD)
        if cand_dist < UPDATE_COSINE:
            new_tokens = _tokenize(new_content)
            old_tokens = _tokenize(cand_doc)
            overlap = _jaccard(new_tokens, old_tokens)
            diagnostics["top_overlap"] = round(overlap, 3)

            # Semantically similar but lexically different = refinement/update
            if overlap < TOKEN_OVERLAP_MAX and new_confidence >= (cand_conf - 0.1):
                return (
                    "UPDATE",
                    cand_id,
                    {**diagnostics, "reason": "semantic match with refinement"},
                )

            # High token overlap + close embedding + lower confidence = NOOP
            if overlap >= TOKEN_OVERLAP_MAX and new_confidence <= cand_conf:
                return ("NOOP", None, {**diagnostics, "reason": "lexical duplicate"})

        # Preference-specific: relaxed cosine + subject overlap
        if is_pref and cand_dist < PREFERENCE_UPDATE_COSINE:
            new_subj = _extract_preference_subject(new_content)
            old_subj = _extract_preference_subject(cand_doc)
            subj_overlap = len(new_subj & old_subj) / max(len(new_subj | old_subj), 1)
            diagnostics["subj_overlap"] = round(subj_overlap, 3)

            if subj_overlap > 0 or cand_dist < UPDATE_COSINE:
                return (
                    "UPDATE",
                    cand_id,
                    {**diagnostics, "reason": "preference topic match"},
                )

        # This candidate wasn't close enough — since results are distance-sorted,
        # later candidates are even farther away. Break out to ADD.
        break

    # All close candidates were already superseded, or none were close enough
    return ("ADD", None, {"reason": "no viable near-duplicate candidate"})


def should_delete_by_content(new_content: str) -> bool:
    """Heuristic: does this memory explicitly invalidate a prior fact?

    Only triggers on narrow, unambiguous invalidation phrasing. Conservative
    by design — false positives are worse than false negatives here, since
    a DELETE that misses still leaves the old fact in place, but a wrong
    DELETE silently removes data.

    Matches:
      "Chris no longer uses X"
      "X is no longer true"
      "X has been replaced by Y"
      "X is invalidated"
      "forget X"
    """
    lowered = (new_content or "").lower()
    patterns = [
        r"\bno longer (use|uses|using|have|has|had|need|needs|needed|own|owns)\b",
        r"\bis no longer (true|valid|correct|accurate|current)\b",
        r"\bhas been (replaced|superseded|invalidated)\b",
        r"\bforget\s+(that|the|this)\b",
        r"\bnever mind\s+(that|the)\b",
    ]
    return any(re.search(p, lowered) for p in patterns)
