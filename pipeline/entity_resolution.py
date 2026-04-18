#!/opt/homebrew/bin/python3
"""Nightly entity resolution — merge near-duplicate entities using embedding similarity.

Finds entity pairs with:
  1. High embedding similarity (>0.90 cosine)
  2. Co-occurrence count > 0

Proposes merges for human review or auto-merges high-confidence pairs.

Usage:
  entity_resolution.py              # dry-run (show proposals)
  entity_resolution.py --apply      # auto-merge high-confidence pairs (>0.95 similarity)
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "brain_core"))


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def find_merge_candidates(min_mentions: int = 3, similarity_threshold: float = 0.90) -> list[dict]:
    """Find entity pairs that might be the same real-world entity."""
    from neo4j_client import run_query
    from search import get_embedding

    # Get entities with enough mentions to be worth merging
    entities = run_query(
        "MATCH (e:Entity) WHERE e.mention_count >= $min "
        "RETURN e.name AS name, e.entity_type AS type, e.mention_count AS mentions "
        "ORDER BY e.mention_count DESC LIMIT 200",
        {"min": min_mentions},
    )

    if len(entities) < 2:
        return []

    # Embed all entity names
    name_embeddings = {}
    for e in entities:
        try:
            emb = get_embedding(e["name"], prefix="query")
            name_embeddings[e["name"]] = (emb, e)
        except Exception:
            continue

    # Pairwise cosine similarity
    candidates = []
    names = list(name_embeddings.keys())
    for i, name_a in enumerate(names):
        emb_a, meta_a = name_embeddings[name_a]
        for name_b in names[i + 1 :]:
            emb_b, meta_b = name_embeddings[name_b]

            # Cosine similarity
            dot = sum(a * b for a, b in zip(emb_a, emb_b, strict=False))
            norm_a = sum(a * a for a in emb_a) ** 0.5
            norm_b = sum(b * b for b in emb_b) ** 0.5
            if norm_a == 0 or norm_b == 0:
                continue
            sim = dot / (norm_a * norm_b)

            if sim >= similarity_threshold:
                # Type constraint: don't merge entities of different types
                if meta_a.get("type", "concept") != meta_b.get("type", "concept"):
                    continue

                # Keep the one with more mentions as canonical
                if meta_a["mentions"] >= meta_b["mentions"]:
                    canonical, alias = name_a, name_b
                else:
                    canonical, alias = name_b, name_a

                candidates.append(
                    {
                        "canonical": canonical,
                        "alias": alias,
                        "similarity": round(sim, 4),
                        "canonical_mentions": meta_a["mentions"]
                        if canonical == name_a
                        else meta_b["mentions"],
                        "alias_mentions": meta_b["mentions"] if canonical == name_a else meta_a["mentions"],
                    }
                )

    candidates.sort(key=lambda c: c["similarity"], reverse=True)
    return candidates


PROTECTED_ENTITIES = frozenset(
    {
        "chris cho",
        "jenna",
        "liz",
        "ellie",
        "sage",
        "market",  # people/agents
        "brain",
        "nginx",
        "docker",
        "chromadb",
        "ollama",
        "neo4j",  # core infrastructure
    }
)


def merge_entity(canonical: str, alias: str):
    """Merge alias entity into canonical: transfer edges, add alias, delete alias node."""
    from entity_graph import add_alias
    from neo4j_client import run_write

    now = _now_iso()

    # Transfer outbound edges (alias -> other) preserving direction
    run_write(
        "MATCH (old:Entity {name: $alias})-[r:RELATES_TO]->(other:Entity) "
        "WHERE other.name <> $canonical "
        "WITH r, other "
        "MATCH (new:Entity {name: $canonical}) "
        "MERGE (new)-[nr:RELATES_TO {relationship: r.relationship}]->(other) "
        "ON CREATE SET nr.weight = r.weight, nr.co_occurrence_count = r.co_occurrence_count, "
        "  nr.created_at = $now "
        "ON MATCH SET nr.co_occurrence_count = nr.co_occurrence_count + coalesce(r.co_occurrence_count, 1)",
        {"alias": alias, "canonical": canonical, "now": now},
    )
    # Transfer inbound edges (other -> alias) preserving direction
    run_write(
        "MATCH (other:Entity)-[r:RELATES_TO]->(old:Entity {name: $alias}) "
        "WHERE other.name <> $canonical "
        "WITH r, other "
        "MATCH (new:Entity {name: $canonical}) "
        "MERGE (other)-[nr:RELATES_TO {relationship: r.relationship}]->(new) "
        "ON CREATE SET nr.weight = r.weight, nr.co_occurrence_count = r.co_occurrence_count, "
        "  nr.created_at = $now "
        "ON MATCH SET nr.co_occurrence_count = nr.co_occurrence_count + coalesce(r.co_occurrence_count, 1)",
        {"alias": alias, "canonical": canonical, "now": now},
    )

    # Add alias name
    add_alias(canonical, alias)

    # Transfer mention count. Use separate MATCH to avoid cartesian product warning.
    run_write(
        "MATCH (old:Entity {name: $alias}) MATCH (new:Entity {name: $canonical}) "
        "SET new.mention_count = new.mention_count + old.mention_count",
        {"alias": alias, "canonical": canonical},
    )

    # Delete alias node and its edges
    run_write(
        "MATCH (old:Entity {name: $alias}) DETACH DELETE old",
        {"alias": alias},
    )

    # Cascade: consolidate duplicate edges created by the merge
    _cascade_merge_edges(canonical)


def _cascade_merge_edges(canonical: str):
    """After entity merge, consolidate parallel edges with same relationship type."""
    from neo4j_client import run_write

    # Outbound edges
    run_write(
        "MATCH (a:Entity {name: $name})-[r:RELATES_TO]->(b:Entity) "
        "WITH a, b, r.relationship AS rel, collect(r) AS edges "
        "WHERE size(edges) > 1 "
        "WITH edges[0] AS keep, edges[1..] AS extras "
        "UNWIND extras AS extra "
        "SET keep.co_occurrence_count = coalesce(keep.co_occurrence_count, 1) + coalesce(extra.co_occurrence_count, 1), "
        "    keep.weight = CASE WHEN coalesce(keep.weight, 0.1) + coalesce(extra.weight, 0.1) > 1.0 THEN 1.0 "
        "      ELSE coalesce(keep.weight, 0.1) + coalesce(extra.weight, 0.1) END "
        "DELETE extra",
        {"name": canonical},
    )
    # Inbound edges
    run_write(
        "MATCH (b:Entity)-[r:RELATES_TO]->(a:Entity {name: $name}) "
        "WITH a, b, r.relationship AS rel, collect(r) AS edges "
        "WHERE size(edges) > 1 "
        "WITH edges[0] AS keep, edges[1..] AS extras "
        "UNWIND extras AS extra "
        "SET keep.co_occurrence_count = coalesce(keep.co_occurrence_count, 1) + coalesce(extra.co_occurrence_count, 1), "
        "    keep.weight = CASE WHEN coalesce(keep.weight, 0.1) + coalesce(extra.weight, 0.1) > 1.0 THEN 1.0 "
        "      ELSE coalesce(keep.weight, 0.1) + coalesce(extra.weight, 0.1) END "
        "DELETE extra",
        {"name": canonical},
    )


def run(apply: bool = False, auto_merge_threshold: float = 0.95):
    print(f"Entity resolution — {_now_iso()}")
    candidates = find_merge_candidates()
    print(f"Found {len(candidates)} merge candidates")

    if not candidates:
        return

    # Protected entities always require review, never auto-merge
    auto_merge = []
    review = []
    for c in candidates:
        if c["canonical"] in PROTECTED_ENTITIES or c["alias"] in PROTECTED_ENTITIES:
            review.append(c)  # protected — always manual review
        elif c["similarity"] >= auto_merge_threshold:
            auto_merge.append(c)
        else:
            review.append(c)

    print(f"\nAuto-merge candidates (sim >= {auto_merge_threshold}): {len(auto_merge)}")
    for c in auto_merge:
        print(
            f"  {c['alias']} → {c['canonical']}  sim={c['similarity']}  mentions={c['alias_mentions']}→{c['canonical_mentions']}"
        )

    print(f"\nReview candidates: {len(review)}")
    for c in review:
        print(f"  {c['alias']} ↔ {c['canonical']}  sim={c['similarity']}")

    if apply and auto_merge:
        print(f"\nApplying {len(auto_merge)} auto-merges...")
        for c in auto_merge:
            try:
                merge_entity(c["canonical"], c["alias"])
                print(f"  merged: {c['alias']} → {c['canonical']}")
                try:
                    from audit_log import log_event

                    log_event(
                        event_type="merge",
                        entity_a=c["canonical"],
                        entity_b=c["alias"],
                        match_score=c["similarity"],
                        resolution="auto_merge",
                        reason=f"Embedding similarity {c['similarity']:.4f} >= {auto_merge_threshold}",
                    )
                except Exception:
                    pass
            except Exception as e:
                print(f"  FAILED: {c['alias']} → {c['canonical']}: {e}")
    elif not apply:
        print("\nRun with --apply to execute auto-merges")


if __name__ == "__main__":
    run(apply="--apply" in sys.argv)
