#!/opt/homebrew/bin/python3
"""Backfill top semantic_memory preferences into Neo4j as Preference nodes.

Queries semantic_memory ChromaDB collection for category=preference entries,
creates typed Preference nodes linked to domain entities.

Usage:
  backfill_preferences.py              # dry-run
  backfill_preferences.py --apply      # write to Neo4j
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "brain_core"))

_DOMAIN_KEYWORDS = {
    "coding": {
        "typescript",
        "react",
        "vite",
        "nextjs",
        "python",
        "code",
        "import",
        "function",
        "type",
        "const",
        "strict",
        "eslint",
        "prettier",
        "npm",
    },
    "infra": {
        "docker",
        "nginx",
        "cloudflare",
        "chromadb",
        "ollama",
        "neo4j",
        "container",
        "port",
        "server",
        "deploy",
        "uptime",
        "launchd",
    },
    "personal": {"chris", "schedule", "prefer", "like", "always", "never", "habit", "routine", "timezone"},
    "communication": {"tone", "emoji", "concise", "direct", "response", "message", "slack", "telegram"},
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _infer_domain(text: str) -> str:
    text_lower = text.lower()
    scores = {}
    for domain, keywords in _DOMAIN_KEYWORDS.items():
        scores[domain] = sum(1 for kw in keywords if kw in text_lower)
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "personal"


def collect_preferences() -> list[dict]:
    """Query semantic_memory for preference entries."""
    from vector_store import get_vector_store

    points = get_vector_store().get(
        "semantic_memory",
        filter={"category": "preference"},
        limit=200,
        with_payload=True,
        with_documents=True,
    )
    if not points:
        print("semantic_memory collection empty or missing")
        return []

    prefs = []
    for p in points:
        doc = p.document or ""
        meta = p.payload or {}
        if not doc or len(doc.strip()) < 10:
            continue
        prefs.append(
            {
                "id": p.id,
                "content": doc.strip()[:200],
                "agent": meta.get("agent", ""),
                "confidence": float(meta.get("confidence", "0.5")),
                "domain": _infer_domain(doc),
            }
        )

    # Sort by confidence descending, take top 50
    prefs.sort(key=lambda p: p["confidence"], reverse=True)
    return prefs[:50]


def backfill(apply: bool = False):
    prefs = collect_preferences()
    print(f"Found {len(prefs)} preference entries")

    if not apply:
        print("\n[DRY RUN] Would create:")
        for p in prefs[:15]:
            print(f"  [{p['domain']}] conf={p['confidence']:.2f} {p['content'][:70]}")
        if len(prefs) > 15:
            print(f"  ... and {len(prefs) - 15} more")
        print("\nRun with --apply to write to Neo4j")
        return

    from neo4j_client import run_write

    now = _now_iso()

    created = 0
    for p in prefs:
        # Create a short name from content
        name = re.sub(r"[^a-z0-9\s]", "", p["content"].lower())[:60].strip()
        name = " ".join(name.split()[:8])  # max 8 words
        if len(name) < 5:
            continue

        run_write(
            "MERGE (pref:Entity {name: $name}) "
            "ON CREATE SET pref.id = 'pref_' + left(randomUUID(), 12), "
            "  pref.entity_type = 'preference', pref.first_seen_at = $now, "
            "  pref.last_seen_at = $now, pref.mention_count = 1, "
            "  pref.memory_class = 'permanent', pref.domain = $domain, "
            "  pref.confidence = $confidence, pref.full_content = $content "
            "ON MATCH SET pref.last_seen_at = $now, "
            "  pref.mention_count = pref.mention_count + 1, "
            "  pref.confidence = $confidence",
            {
                "name": name,
                "now": now,
                "domain": p["domain"],
                "confidence": p["confidence"],
                "content": p["content"],
            },
        )

        # Link preference to Chris
        run_write(
            "MATCH (c:Entity {name: 'chris cho'}), (p:Entity {name: $name}) "
            "MERGE (c)-[r:RELATES_TO {relationship: 'prefers'}]->(p) "
            "ON CREATE SET r.weight = $conf, r.co_occurrence_count = 1, r.created_at = $now "
            "ON MATCH SET r.weight = $conf",
            {"name": name, "conf": p["confidence"], "now": now},
        )
        created += 1

    print(f"Created {created} Preference nodes")


if __name__ == "__main__":
    backfill(apply="--apply" in sys.argv)
