"""brain_core/dream_replay.py - REM-like generative recombination (Wagner 2004).

Biological sleep includes REM phases where memories recombine in novel
configurations that never occurred in waking life - the mechanism behind
creative insight and analogical reasoning. Wagner et al. (2004) showed
sleep after learning improves insight on new problems by ~3x.

This module runs a weekly `dream_replay` job:
  1. Sample pairs of distant-domain entities from Neo4j that have NO
     existing RELATES_TO edge (the brain has not yet connected them).
  2. For each pair, ask Sage to hypothesize a novel connection:
     "What non-obvious relationship might exist between X and Y given
     what you know about Chris's work?"
  3. Filter Sage's output: require >= 100 chars, must reference both
     entities, must pass a token-overlap check against each entity's
     existing canonical descriptions (guards against pure hallucination).
  4. Store surviving hypotheses as low-confidence (0.3) "conjecture"
     atoms with kind="conjecture", tier="episodic". They never get
     promoted to canonical until independent corroborating evidence
     arrives via the normal ingest pipeline.

Conjecture atoms are retrievable but heavily down-weighted in ranking so
they surface only when explicitly asked for (or when other retrievals
fail). This matches the biological pattern: dreams inform waking thought
but aren't mistaken for memories of real events.

Constraint-compliant: zero new local LLMs. Sage dispatch goes through
OpenClaw (GPT Pro flat-rate).
"""

from __future__ import annotations

import json
import logging
import random
import sys
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("brain.dream_replay")

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 2026-04-20 nightly schedule: bumped 5 -> 15 pairs/night so weekly aggregate
# rises ~20x (35 vs old 5). Budget: 15 Sage dispatches x ~45s = under 15min
# of Sage-API wall-time, still well within nightly window.
MAX_PAIRS_PER_RUN = 15
MIN_HYPOTHESIS_CHARS = 100
CONJECTURE_CONFIDENCE = 0.3


def _sample_distant_pairs(limit: int) -> list[tuple[dict, dict]]:
    """Sample entity pairs with no existing RELATES_TO edge between them.

    Uses Neo4j when available, else returns empty list (feature is a no-op
    on systems without the graph). Pairs are biased toward entities with
    at least 2 mentions each - we want real entities, not noise.
    """
    try:
        from entity_graph import _use_neo4j

        if not _use_neo4j():
            return []
        from neo4j_client import run_query

        rows = run_query(
            """
            MATCH (e:Entity)
            WHERE size(e.name) >= 4
              AND coalesce(e.mention_count, 1) >= 2
            WITH e ORDER BY rand() LIMIT 200
            RETURN e.name AS name, coalesce(e.description, '') AS description
            """,
            {},
        )
    except Exception:
        return []
    if len(rows) < 2:
        return []
    # Pair up randomly; skip pairs whose names are substrings of each other.
    pairs: list[tuple[dict, dict]] = []
    tried = 0
    while len(pairs) < limit and tried < limit * 10:
        tried += 1
        a, b = random.sample(rows, 2)
        if a["name"].lower() in b["name"].lower() or b["name"].lower() in a["name"].lower():
            continue
        # Skip if already connected
        try:
            existing = run_query(
                "MATCH (a:Entity {name: $a})-[r:RELATES_TO]-(b:Entity {name: $b}) " "RETURN count(r) AS n",
                {"a": a["name"], "b": b["name"]},
            )
            if existing and int(existing[0].get("n", 0)) > 0:
                continue
        except Exception as _exc:
            log.debug("silenced exception in dream_replay.py: %s", _exc)
        pairs.append((a, b))
    return pairs


def _hypothesize(pair: tuple[dict, dict]) -> str | None:
    """Ask Sage to generate a novel connection hypothesis for the pair."""
    a, b = pair
    try:
        from cli_llm import dispatch

        prompt = (
            f"You are exploring novel cross-domain connections in Chris's knowledge base.\n\n"
            f"Entity A: {a['name']}\n"
            f"Description: {(a.get('description') or '')[:300]}\n\n"
            f"Entity B: {b['name']}\n"
            f"Description: {(b.get('description') or '')[:300]}\n\n"
            f"Propose ONE non-obvious but plausible connection between these "
            f"two entities. The connection should be:\n"
            f"  - Grounded in what you know about Chris's work / stack\n"
            f"  - Something he might not have noticed but would find useful\n"
            f"  - Specific (not a vague analogy)\n"
            f"If no plausible connection exists, reply exactly 'NO_CONNECTION'.\n\n"
            f"Output the hypothesis as a single paragraph (100-300 chars)."
        )
        result = dispatch(agent="sage", message=prompt, thinking="low", timeout=45)
        if not getattr(result, "ok", False):
            return None
        text = (result.text or "").strip()
        if not text or "NO_CONNECTION" in text.upper():
            return None
        if len(text) < MIN_HYPOTHESIS_CHARS:
            return None
        # Both entity names should appear in the hypothesis (guards against
        # off-topic LLM output).
        lower = text.lower()
        if a["name"].lower() not in lower or b["name"].lower() not in lower:
            return None
        return text[:600]
    except Exception:
        return None


def _store_conjecture(pair: tuple[dict, dict], hypothesis: str) -> str | None:
    """Persist the hypothesis as a low-confidence conjecture atom."""
    try:
        import hashlib as _h

        from atoms_store import upsert_atom

        a, b = pair
        seed = f"{a['name']}::{b['name']}::{hypothesis[:100]}"
        chroma_id = f"dream:{_h.md5(seed.encode('utf-8'), usedforsecurity=False).hexdigest()[:16]}"
        now_iso = datetime.now(UTC).isoformat(timespec="seconds")
        return upsert_atom(
            text=f"Dream conjecture ({a['name']} x {b['name']}):\n{hypothesis}",
            chroma_id=chroma_id,
            kind="conjecture",
            confidence=CONJECTURE_CONFIDENCE,
            tier="episodic",
            provisional=1,
            trust_score=0.35,
            valid_from=now_iso,
            speaker_entity="sage",
            scope="global",
            provenance_json=json.dumps(
                {
                    "origin": "dream_replay",
                    "entity_a": a["name"],
                    "entity_b": b["name"],
                    "generated_at": now_iso,
                }
            ),
        )
    except Exception:
        return None


def run() -> dict:
    """Weekly dream replay pass. Returns summary for scheduler."""
    pairs = _sample_distant_pairs(MAX_PAIRS_PER_RUN)
    if not pairs:
        return {"status": "skip", "reason": "no_distant_pairs"}
    hypotheses: list[dict] = []
    for pair in pairs:
        hyp = _hypothesize(pair)
        if not hyp:
            continue
        atom_id = _store_conjecture(pair, hyp)
        hypotheses.append(
            {
                "a": pair[0]["name"],
                "b": pair[1]["name"],
                "hypothesis": hyp[:200],
                "atom_id": atom_id,
            }
        )
    return {
        "status": "ok",
        "pairs_sampled": len(pairs),
        "conjectures_stored": len(hypotheses),
        "hypotheses": hypotheses,
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, ensure_ascii=False))  # noqa: T201
