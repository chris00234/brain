"""brain_core/community_summaries.py — GraphRAG community summaries (M8.5).

Microsoft GraphRAG (arXiv:2404.16130) showed that pre-summarizing entity
clusters lets RAG answer "global" questions ("what's everything I know about
X domain", "give me a summary of the Y system") that single-doc retrieval
can't handle. The brain has strong single-doc retrieval (98.6% stable
content_hit) but cross-document synthesis is weak — it's the reason WS9's
COMMERCIAL_READINESS.md flagged "no cross-document synthesis" as a remaining
gap.

Pipeline:
  1. Pull all (a)-[:RELATES_TO]->(b) triples from Neo4j entity graph
  2. Build a networkx Graph weighted by edge weight
  3. Run Louvain community detection (greedy modularity maximization)
  4. For each community of >= MIN_COMMUNITY_SIZE entities, gather all
     atoms that mention any entity in the community
  5. Dispatch to Sage for a 3-5 sentence summary per community
  6. Store summary in `community_summaries` table (brain.db)

Wire-up:
  - Weekly job `community_summaries` Sun 5:00am (after profile_regen 4:00,
    before pdf_ingest 5:30 — fits the off-hours pipeline)
  - search_unified can pre-fetch summaries for queries classified as MULTI
    by adaptive_rag.classify (M8.4) — done as a follow-up

Module-level kill switch: BRAIN_COMMUNITY_SUMMARIES env var (default off
until measured). The Neo4j read + Louvain are cheap (~1s); the LLM dispatch
is the cost (~10-20 communities x 5s x $0.0005 = ~$0.05-0.1 per weekly run).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

log = logging.getLogger("brain.community_summaries")

ENABLED = os.environ.get("BRAIN_COMMUNITY_SUMMARIES", "").lower() in {"1", "true", "yes"}

MIN_COMMUNITY_SIZE = 3  # skip 1-2 entity "communities"
MAX_COMMUNITIES = 20  # cap LLM dispatches per run
MAX_ATOMS_PER_COMMUNITY = 25  # context window guard for Sage
SUMMARY_TIMEOUT_S = 30


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _ensure_schema() -> None:
    try:
        from config import BRAIN_DB

        conn = sqlite3.connect(str(BRAIN_DB))
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS community_summaries (
                  id              INTEGER PRIMARY KEY AUTOINCREMENT,
                  community_hash  TEXT NOT NULL UNIQUE,
                  entities_json   TEXT NOT NULL,
                  summary         TEXT NOT NULL,
                  atom_count      INTEGER NOT NULL DEFAULT 0,
                  generated_at    TEXT NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_community_summaries_generated "
                "ON community_summaries(generated_at DESC)"
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        log.warning("community_summaries schema init failed: %s", exc)


def _load_edges_from_neo4j() -> list[tuple[str, str, float]]:
    """Pull weighted edges. Returns list of (a, b, weight)."""
    try:
        from neo4j_client import run_query

        rows = run_query(
            "MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity) "
            "WHERE a.mention_count > 1 AND b.mention_count > 1 "
            "RETURN a.name AS a, b.name AS b, coalesce(r.weight, 1.0) AS w "
            "ORDER BY w DESC "
            "LIMIT 5000"
        )
        return [(str(r["a"]), str(r["b"]), float(r["w"])) for r in rows if r.get("a") and r.get("b")]
    except Exception as exc:
        log.warning("community_summaries neo4j fetch failed: %s", exc)
        return []


def _detect_communities(edges: list[tuple[str, str, float]]) -> list[set[str]]:
    """Run Louvain community detection on the weighted entity graph."""
    if not edges:
        return []
    try:
        import networkx as nx
        from networkx.algorithms.community import louvain_communities

        g = nx.Graph()
        for a, b, w in edges:
            g.add_edge(a, b, weight=w)

        communities = louvain_communities(g, seed=42)
        return [set(c) for c in communities if len(c) >= MIN_COMMUNITY_SIZE]
    except Exception as exc:
        log.warning("louvain detection failed: %s", exc)
        return []


def _gather_atoms_for_community(entity_names: set[str]) -> list[str]:
    """Pull recent atoms whose text contains any entity name. Lightweight LIKE
    join — exact precision isn't needed because Sage will filter via summary.
    """
    try:
        from config import BRAIN_DB

        conn = sqlite3.connect(str(BRAIN_DB))
        try:
            placeholders = " OR ".join(["text LIKE ?"] * len(entity_names))
            params = [f"%{name}%" for name in entity_names]
            rows = conn.execute(
                f"SELECT text FROM atoms WHERE tier IN ('semantic', 'core') "  # noqa: S608
                f"AND ({placeholders}) "
                f"ORDER BY confidence DESC LIMIT {MAX_ATOMS_PER_COMMUNITY}",
                params,
            ).fetchall()
            return [r[0] for r in rows if r[0]]
        finally:
            conn.close()
    except Exception as exc:
        log.warning("atom gather failed: %s", exc)
        return []


_SUMMARY_PROMPT = """You are summarizing a cluster of related concepts in Chris's personal knowledge base.

Concept cluster (entities that frequently appear together):
{entities}

Atoms (concise facts mentioning these concepts):
{atoms}

Write a 3-5 sentence summary that captures:
1. What this cluster is about (the unifying theme)
2. The most important relationships between the concepts
3. Any non-obvious connections a reader should know

Output ONLY the summary text. No preamble, no markdown headers, no bullet lists."""


def _summarize_community(entities: set[str], atoms: list[str]) -> str | None:
    """Dispatch to Sage for a community summary."""
    if not atoms:
        return None
    try:
        from openclaw_dispatch import dispatch

        prompt = _SUMMARY_PROMPT.format(
            entities=", ".join(sorted(entities)[:30]),
            atoms="\n".join(f"- {a[:300]}" for a in atoms),
        )
        result = dispatch("sage", prompt, thinking="off", timeout=SUMMARY_TIMEOUT_S)
        if result.ok and result.text:
            return result.text.strip()
    except Exception as exc:
        log.warning("community summary dispatch failed: %s", exc)
    return None


def _community_hash(entities: set[str]) -> str:
    """Stable hash over the sorted entity set."""
    import hashlib

    return hashlib.sha256("|".join(sorted(entities)).encode()).hexdigest()[:16]


def _persist_summary(community_hash: str, entities: set[str], summary: str, atom_count: int) -> bool:
    try:
        from config import BRAIN_DB

        conn = sqlite3.connect(str(BRAIN_DB))
        try:
            conn.execute(
                "INSERT OR REPLACE INTO community_summaries "
                "(community_hash, entities_json, summary, atom_count, generated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (community_hash, json.dumps(sorted(entities)), summary, atom_count, _now_iso()),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as exc:
        log.warning("persist summary failed: %s", exc)
        return False


def get_summaries_matching(query: str, limit: int = 3) -> list[dict]:
    """Return community summaries whose entity set overlaps with terms in the query.

    Lightweight LIKE join — used by search_unified for MULTI-class queries.
    Returns [{"summary": ..., "entities": [...], "generated_at": ...}, ...].
    """
    if not ENABLED or not query:
        return []
    try:
        from config import BRAIN_DB

        conn = sqlite3.connect(str(BRAIN_DB))
        try:
            rows = conn.execute(
                "SELECT entities_json, summary, atom_count, generated_at "
                "FROM community_summaries "
                "WHERE generated_at IS NOT NULL "
                "ORDER BY generated_at DESC LIMIT 100"
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []

    # M8.7: word-level overlap match instead of strict substring. Previously
    # `"brain system" in "compare brain and homelab"` was False because the
    # multi-word entity didn't appear contiguously, even though "brain" clearly
    # links this query to the brain-system cluster. Now: tokenize the query,
    # tokenize each entity, and match if ANY entity word of length >= 4 is in
    # the query tokens. The length floor avoids "ai"/"pr" false positives.
    import re as _re

    q_tokens = {t for t in _re.findall(r"[\w가-힣]+", query.lower()) if len(t) >= 4}

    matches: list[dict] = []
    for entities_json, summary, atom_count, generated_at in rows:
        try:
            entities = json.loads(entities_json)
        except (json.JSONDecodeError, TypeError):
            continue
        entity_tokens: set[str] = set()
        for e in entities:
            for t in _re.findall(r"[\w가-힣]+", e.lower()):
                if len(t) >= 4:
                    entity_tokens.add(t)
        if q_tokens & entity_tokens:
            matches.append(
                {
                    "summary": summary,
                    "entities": entities,
                    "atom_count": atom_count,
                    "generated_at": generated_at,
                    "matched_tokens": sorted(q_tokens & entity_tokens),
                }
            )
            if len(matches) >= limit:
                break
    return matches


def run() -> dict:
    """Entrypoint for the scheduler."""
    if not ENABLED:
        return {"status": "disabled", "reason": "BRAIN_COMMUNITY_SUMMARIES env not set"}

    _ensure_schema()

    edges = _load_edges_from_neo4j()
    if not edges:
        return {"status": "no_edges"}

    communities = _detect_communities(edges)
    if not communities:
        return {"status": "no_communities", "edges": len(edges)}

    # Order by size desc, cap at MAX_COMMUNITIES
    communities = sorted(communities, key=len, reverse=True)[:MAX_COMMUNITIES]

    summarized = 0
    skipped = 0
    for community in communities:
        atoms = _gather_atoms_for_community(community)
        if not atoms:
            skipped += 1
            continue
        summary = _summarize_community(community, atoms)
        if not summary:
            skipped += 1
            continue
        if _persist_summary(_community_hash(community), community, summary, len(atoms)):
            summarized += 1
        else:
            skipped += 1

    return {
        "status": "ok",
        "edges": len(edges),
        "communities_detected": len(communities),
        "summarized": summarized,
        "skipped": skipped,
    }


def stats() -> dict:
    return {
        "enabled": ENABLED,
        "min_community_size": MIN_COMMUNITY_SIZE,
        "max_communities": MAX_COMMUNITIES,
        "max_atoms_per_community": MAX_ATOMS_PER_COMMUNITY,
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, ensure_ascii=False))  # noqa: T201
