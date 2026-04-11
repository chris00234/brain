"""brain_core/memory_operations.py — Mem0-inspired operation semantics.

Classify incoming memories as ADD / UPDATE / DELETE / NOOP before storing.
Prevents memory accumulation without intent over long time horizons.
"""
from __future__ import annotations

import logging
import re
from typing import Literal, Optional

from http_pool import http_json

log = logging.getLogger("brain.memory_operations")

Operation = Literal["ADD", "UPDATE", "DELETE", "NOOP"]

# Thresholds tuned from audit data
DUPLICATE_COSINE = 0.05       # near-exact match → NOOP
UPDATE_COSINE = 0.15          # semantic similarity → potential UPDATE
TOKEN_OVERLAP_MAX = 0.7       # below this + high semantic sim → refinement (UPDATE)

_WORD_RE = re.compile(r"[a-z0-9_\-]{3,}")


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
    sem_col_id: str,
    chroma_url: str = "http://127.0.0.1:8000",
) -> tuple[Operation, Optional[str], dict]:
    """Classify a new memory against existing ones.

    Returns:
        (operation, superseded_id_if_update, diagnostics)

    Diagnostics dict contains: top_distance, top_overlap, top_confidence, reason
    """
    if not new_embedding:
        return ("ADD", None, {"reason": "no embedding available"})

    # Query top 3 similar memories
    try:
        resp = http_json(
            "POST",
            f"{chroma_url}/api/v2/tenants/default_tenant/databases/default_database/collections/{sem_col_id}/query",
            payload={
                "query_embeddings": [new_embedding],
                "n_results": 3,
                "include": ["documents", "metadatas", "distances"],
            },
            timeout=15,
        )
    except Exception as e:
        log.debug("classify: query failed (%s) — defaulting to ADD", e)
        return ("ADD", None, {"reason": f"query failed: {e}"})

    distances = (resp.get("distances") or [[]])[0]
    ids = (resp.get("ids") or [[]])[0]
    docs = (resp.get("documents") or [[]])[0]
    metas = (resp.get("metadatas") or [[]])[0]

    if not ids:
        return ("ADD", None, {"reason": "no similar memories"})

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

        # Exact duplicate — very close + same-or-higher existing confidence
        if cand_dist < DUPLICATE_COSINE and cand_conf >= new_confidence:
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
