"""brain_core/eval_holdout_promote.py - weekly eval auto-growth pipeline (Phase C1).

Reads candidate proposals from `eval_proposals` table, scores them by novelty
against the existing eval_set.json, drops near-duplicates, and writes the top-N
to a pending file for human audit.

Schedule: Sun 8:45am via JOB_REGISTRY/scheduler (registered separately).

Novelty scoring: 1 - max cosine similarity against existing eval set queries
using the local Ollama embedder. No new API spend (uses existing rail).
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

log = logging.getLogger("brain.eval_holdout_promote")

try:
    from config import AUTONOMY_DB, BRAIN_DIR
    from eval_proposals import list_candidates, mark_status
except ImportError:
    AUTONOMY_DB = Path("/Users/chrischo/server/brain/logs/autonomy.db")
    BRAIN_DIR = Path("/Users/chrischo/server/brain")
    list_candidates = None  # type: ignore[assignment]
    mark_status = None  # type: ignore[assignment]


EVAL_SET_PATH = BRAIN_DIR / "cli" / "eval_set.json"
PENDING_PATH = BRAIN_DIR / "cli" / "eval_holdout_pending.json"

NOVELTY_THRESHOLD = 0.30  # below this similarity = novel enough to promote
TOP_N = 5  # max items to promote per weekly run


def _embed(text: str) -> list[float] | None:
    """Embed via the local Ollama embedder. Returns None on failure.

    indexer exposes `get_embedding(text)` for single texts and
    `get_embeddings_batch(texts)` for batches. The previous import of
    `_embed_texts` was a stale name and silently broke the M7 self-evolution
    loop (caught by tests/integration/test_self_evolution_e2e.py).
    """
    try:
        from indexer import get_embedding  # type: ignore[attr-defined]

        result = get_embedding(text, prefix="query")
        if result and isinstance(result, list):
            return result
    except Exception as exc:
        log.warning("embed failed: %s", exc)
    return None


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _load_eval_queries() -> list[str]:
    """Load existing eval queries to compare novelty against."""
    if not EVAL_SET_PATH.exists():
        return []
    try:
        data = json.loads(EVAL_SET_PATH.read_text())
        return [item.get("query", "") for item in data if item.get("query")]
    except Exception as exc:
        log.warning("failed to load eval set: %s", exc)
        return []


def _max_similarity_to_existing(query_emb: list[float], existing_embs: list[list[float]]) -> float:
    return max((_cosine_similarity(query_emb, e) for e in existing_embs), default=0.0)


def run() -> dict:
    """Walk candidate proposals, compute novelty, promote top-N to pending file."""
    if list_candidates is None or mark_status is None:
        return {"error": "eval_proposals module unavailable"}

    candidates = list_candidates(status="candidate", limit=200)
    if not candidates:
        return {"checked": 0, "promoted": 0, "rejected": 0, "reason": "no candidates"}

    existing_queries = _load_eval_queries()
    if not existing_queries:
        log.warning("no existing eval queries to compare against — promoting all candidates as novel")
        existing_embs: list[list[float]] = []
    else:
        # Embed existing queries once (could be cached, but rebuild weekly is fine)
        existing_embs = []
        for q in existing_queries:
            emb = _embed(q)
            if emb:
                existing_embs.append(emb)

    scored: list[tuple[float, dict]] = []
    rejected = 0
    for cand in candidates:
        cand_query = cand.get("query") or ""
        if not cand_query:
            continue
        cand_emb = _embed(cand_query)
        if not cand_emb:
            continue
        max_sim = _max_similarity_to_existing(cand_emb, existing_embs)
        novelty = 1.0 - max_sim
        if novelty < NOVELTY_THRESHOLD:
            mark_status(cand["id"], "rejected", novelty_score=novelty)
            rejected += 1
            continue
        scored.append((novelty, cand))

    # Top-N by novelty
    scored.sort(key=lambda x: x[0], reverse=True)
    promoted = scored[:TOP_N]

    pending_payload = []
    for novelty, cand in promoted:
        pending_payload.append(
            {
                "id": cand["id"],
                "query": cand["query"],
                "expected": cand["expected"],
                "expected_sources": json.loads(cand.get("expected_sources") or "[]"),
                "novelty": round(novelty, 3),
                "source_event": cand.get("source_event"),
            }
        )
        mark_status(cand["id"], "pending", novelty_score=novelty)

    PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    PENDING_PATH.write_text(json.dumps(pending_payload, indent=2, ensure_ascii=False))

    return {
        "checked": len(candidates),
        "promoted": len(promoted),
        "rejected": rejected,
        "pending_file": str(PENDING_PATH),
    }


if __name__ == "__main__":
    sys.stdout.write(json.dumps(run(), indent=2) + "\n")
