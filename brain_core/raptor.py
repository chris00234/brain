"""brain_core/raptor.py — Recursive Abstractive Processing for Tree-Organized Retrieval.

2026-04-16 Tier 3 #9: Sarthi et al. 2024 RAPTOR. The canonical layer
currently stores notes as a flat collection. Multi-hop and broad-topic
queries are forced to reconstruct patterns from primitives at every read
— the answer to "what's Chris's overall infrastructure philosophy?" has
to aggregate 40+ canonical notes on the fly. RAPTOR builds a tree of
progressively more-abstract summaries: leaf level = canonical notes,
level 1 = clusters of notes, level 2 = clusters of clusters, etc.
Retrieval then navigates the tree at the right abstraction level for
the query.

This module builds the tree offline (weekly) and stores it as a parallel
collection `canonical_raptor` with `level` metadata. At read time,
search_unified can route broad queries to level-2/3 summaries and
specific queries to level-0 canonical.

Tree construction:
  1. Load all active canonical note embeddings from Chroma canonical.
  2. Cluster by agglomerative similarity (sklearn-free): simple greedy
     complete-link over cosine (threshold-based, no k to pick).
  3. For each cluster, dispatch Sage to produce a concise summary.
  4. Embed each summary with passage prefix; upsert into canonical_raptor
     with metadata.level=1, children=[leaf_ids].
  5. Recurse until cluster count <= MAX_ROOT_NODES.

Capped at MAX_LEVELS to keep Sage dispatch bounded. Weekly job runs
after canonical_compaction Sunday 06:00 so it sees the freshest state.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


# Conservative thresholds to avoid sprawl: maintain ~10-30 level-1
# summaries, ~3-8 level-2, ~1-2 level-3 for typical canonical size.
MAX_LEVELS = 3
CLUSTER_SIM_THRESHOLD = 0.75
MIN_CLUSTER_SIZE = 2
MAX_CLUSTER_SIZE = 12
MAX_ROOT_NODES = 4
COLLECTION_NAME = "canonical_raptor"


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _cluster_by_similarity(items: list[dict], threshold: float, max_size: int) -> list[list[int]]:
    """Greedy agglomerative clustering.

    items: [{"embedding": [...], "text": ...}]. Returns list of index
    groups. Complete-link-ish: an item joins a cluster if it's above
    threshold to ALL current members.
    """
    clusters: list[list[int]] = []
    for i, item in enumerate(items):
        emb = item.get("embedding") or []
        if not emb:
            clusters.append([i])
            continue
        placed = False
        for c in clusters:
            if len(c) >= max_size:
                continue
            min_sim = 1.0
            for j in c:
                sim = _cosine(emb, items[j].get("embedding") or [])
                if sim < min_sim:
                    min_sim = sim
                if min_sim < threshold:
                    break
            if min_sim >= threshold:
                c.append(i)
                placed = True
                break
        if not placed:
            clusters.append([i])
    return clusters


def _summarize_cluster_via_sage(texts: list[str], level: int) -> str | None:
    """Dispatch Sage to produce a concise summary of a cluster of notes."""
    try:
        from cli_llm import dispatch

        joined = "\n\n---\n\n".join(t[:800] for t in texts[:MAX_CLUSTER_SIZE])
        prompt = (
            f"Summarize these {len(texts)} related knowledge notes at level {level} "
            f"of an abstraction tree. The summary should:\n"
            f"  - Preserve every distinct factual claim (do not merge away facts).\n"
            f"  - Surface the common theme linking these notes.\n"
            f"  - Be 150-400 words; markdown allowed.\n"
            f"  - Output ONLY the summary text; no preamble, no commentary.\n\n"
            f"NOTES:\n\n{joined}"
        )
        result = dispatch(agent="sage", message=prompt, thinking="low", timeout=90)
        if not getattr(result, "ok", False):
            return None
        text = (result.text or "").strip()
        if len(text) < 120:
            return None
        return text
    except Exception:
        return None


def _load_active_canonical() -> list[dict]:
    """Pull leaf canonical notes + embeddings via the vector store.

    Previously filtered by `status=active` but canonical rarely populates
    `status` unless a note is superseded, so the old filter matched zero
    rows and raptor_build always skipped. Now: pull everything, exclude
    superseded + raptor-summary rows in Python. Qdrant `$gt`/`$lte` on
    missing-field rows don't behave as MongoDB does, so server-side is
    the wrong place to filter.
    """
    try:
        from vector_store import get_vector_store

        points = get_vector_store().get(
            "canonical",
            limit=20000,
            with_payload=True,
            with_documents=True,
            with_vectors=True,
        )
    except Exception:
        return []
    leaves = []
    for p in points:
        meta = p.payload or {}
        if meta.get("status") == "superseded":
            continue
        raptor_level = meta.get("raptor_level")
        if isinstance(raptor_level, int) and raptor_level > 0:
            continue
        leaves.append(p)
    points = leaves
    out = []
    for p in points:
        out.append(
            {
                "id": p.id,
                "embedding": p.vector or [],
                "text": (p.document or "")[:3000],
                "metadata": p.payload or {},
                "level": 0,
            }
        )
    return out


def _ensure_raptor_collection() -> str:
    """Create the canonical_raptor collection if missing; return its name."""
    from vector_store import get_vector_store

    get_vector_store().create_collection(COLLECTION_NAME)
    return COLLECTION_NAME


def _upsert_summary(col_id: str, node_id: str, text: str, level: int, children: list[str]) -> None:
    from indexer import get_embedding
    from vector_store import get_vector_store

    emb = get_embedding(text, use_cache=True, prefix="passage")
    if not emb:
        return
    get_vector_store().upsert(
        col_id,
        ids=[node_id],
        vectors=[emb],
        documents=[text],
        payloads=[
            {
                "type": "raptor-summary",
                "raptor_level": level,
                "children_count": len(children),
                "children": json.dumps(children[:30]),
                "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
        ],
    )


def build_tree() -> dict:
    """Run a full RAPTOR tree build. Idempotent — overwrites previous node ids."""
    leaves = _load_active_canonical()
    if len(leaves) < 2 * MIN_CLUSTER_SIZE:
        return {"status": "skip", "reason": "too few canonical notes", "n": len(leaves)}
    col_id = _ensure_raptor_collection()
    if not col_id:
        return {"status": "error", "reason": "could not create canonical_raptor collection"}

    level_stats: list[dict] = []
    current_level = leaves  # each item: {"id", "embedding", "text", "level"}
    summaries_made = 0

    for level in range(1, MAX_LEVELS + 1):
        if len(current_level) <= MAX_ROOT_NODES:
            break
        clusters = _cluster_by_similarity(current_level, CLUSTER_SIM_THRESHOLD, MAX_CLUSTER_SIZE)
        next_level: list[dict] = []
        made_this_level = 0
        for ci, cluster_idxs in enumerate(clusters):
            if len(cluster_idxs) < MIN_CLUSTER_SIZE:
                # Singleton — pass through unchanged to next level as-is
                next_level.append(current_level[cluster_idxs[0]])
                continue
            cluster_texts = [current_level[i]["text"] for i in cluster_idxs]
            cluster_child_ids = [current_level[i]["id"] for i in cluster_idxs]
            summary = _summarize_cluster_via_sage(cluster_texts, level)
            if not summary:
                # Fall back to passing the most central child through
                next_level.append(current_level[cluster_idxs[0]])
                continue
            node_id = f"raptor:L{level}:{ci:03d}:{datetime.now(UTC).strftime('%Y%m%d')}"
            _upsert_summary(col_id, node_id, summary, level, cluster_child_ids)
            summaries_made += 1
            made_this_level += 1
            # Re-embed so the next level's clustering sees the actual
            # summary-level semantics rather than a stale leaf embedding.
            try:
                from indexer import get_embedding

                emb = get_embedding(summary, use_cache=True, prefix="passage")
            except Exception:
                emb = []
            next_level.append(
                {
                    "id": node_id,
                    "embedding": emb or [],
                    "text": summary,
                    "level": level,
                }
            )
        level_stats.append(
            {
                "level": level,
                "input_count": len(current_level),
                "cluster_count": len(clusters),
                "summaries_made": made_this_level,
            }
        )
        current_level = next_level
        if made_this_level == 0:
            break

    return {
        "status": "ok",
        "leaf_count": len(leaves),
        "levels_built": len(level_stats),
        "summaries_made_total": summaries_made,
        "per_level": level_stats,
    }


if __name__ == "__main__":
    print(json.dumps(build_tree(), indent=2, ensure_ascii=False))  # noqa: T201 — CLI stdout
