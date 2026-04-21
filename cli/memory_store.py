#!/opt/homebrew/bin/python3
"""Semantic memory store — persistent vector-backed memory from the CLI.

Usage:
  memory_store.py store <text> [--agent <name>] [--category fact|preference|decision|entity|other] [--importance 0-1]
  memory_store.py search <query> [--limit 5] [--agent <name>] [--json]
  memory_store.py forget <id_or_query>
  memory_store.py stats [--agent <name>]
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
from search import get_embedding
from vector_store import get_vector_store

COLLECTION = "semantic_memory"
DUPLICATE_THRESHOLD = 0.05  # cosine distance; equivalent similarity floor = 1 - 0.05


def cmd_store(args):
    store = get_vector_store()
    store.create_collection(COLLECTION)

    text = args.text
    agent = args.agent or "unknown"
    category = args.category or "other"
    importance = args.importance or 0.5

    embedding = get_embedding(text)

    hits = store.query(
        COLLECTION, vector=embedding, k=1, with_payload=False, with_vectors=False
    )
    # similarity floor: a hit closer than DUPLICATE_THRESHOLD in cosine-distance
    # equals a similarity score above (1 - DUPLICATE_THRESHOLD).
    sim_floor = 1.0 - DUPLICATE_THRESHOLD
    if hits and hits[0].score > sim_floor:
        print(f"Duplicate detected (similarity: {hits[0].score:.4f}). Skipping.")
        return

    doc_id = f"mem:{agent}:{hashlib.md5(text.encode()).hexdigest()}"[:63]
    now = datetime.now().isoformat()

    store.upsert(
        COLLECTION,
        ids=[doc_id],
        vectors=[embedding],
        documents=[text],
        payloads=[
            {
                "source": f"memory_store:{agent}",
                "agent": agent,
                "type": "semantic-memory",
                "category": category,
                "importance": str(importance),
                "service": "",
                "section": "",
                "created_at": now,
            }
        ],
    )
    print(f"Stored: {text[:80]}... [agent={agent}, category={category}]")


def cmd_search(args):
    store = get_vector_store()
    embedding = get_embedding(args.query, prefix="query")
    n = args.limit or 5

    where = {"agent": {"$eq": args.agent}} if args.agent else None
    hits = store.query(
        COLLECTION, vector=embedding, k=n, filter=where, with_payload=True
    )

    results = []
    for h in hits:
        meta = h.payload or {}
        results.append(
            {
                "content": h.document or "",
                "score": round(h.score, 4),
                "agent": meta.get("agent", ""),
                "category": meta.get("category", ""),
                "created_at": meta.get("created_at", ""),
                "source": meta.get("source", ""),
            }
        )

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))  # noqa: T201 — CLI stdout
    else:
        if not results:
            print("No memories found.")  # noqa: T201 — CLI stdout
            return
        for i, r in enumerate(results):
            print(f"#{i+1} (score: {r['score']:.3f}) [{r['category']}] {r['agent']}")  # noqa: T201
            print(f"  {r['content'][:200]}")  # noqa: T201
            print()  # noqa: T201


def cmd_forget(args):
    store = get_vector_store()
    target = args.id_or_query

    if target.startswith("mem:"):
        store.delete(COLLECTION, ids=[target])
        print(f"Deleted: {target}")
        return

    embedding = get_embedding(target)
    hits = store.query(
        COLLECTION, vector=embedding, k=1, with_payload=True, with_vectors=False
    )
    # Same 0.3 cosine-distance cutoff as before: similarity >= 0.7
    if not hits or hits[0].score < 0.7:
        print(f"No close match found for: {target}")
        return

    hit = hits[0]
    store.delete(COLLECTION, ids=[hit.id])
    doc_preview = (hit.document or "")[:80]
    print(f"Deleted: {hit.id} — {doc_preview}...")


def cmd_stats(args):
    store = get_vector_store()

    if args.agent:
        points = store.get(
            COLLECTION,
            filter={"agent": {"$eq": args.agent}},
            limit=10000,
            with_payload=False,
            with_documents=False,
        )
        print(f"Semantic memories (agent={args.agent}): {len(points)}")
    else:
        total = store.count(COLLECTION)
        print(f"Semantic memories: {total}")


def main():
    parser = argparse.ArgumentParser(description="Semantic Memory Store (VectorStore-backed)")
    sub = parser.add_subparsers(dest="command")

    store_p = sub.add_parser("store", help="Store a memory")
    store_p.add_argument("text", help="Text to store")
    store_p.add_argument("--agent", help="Agent name")
    store_p.add_argument(
        "--category", choices=["fact", "preference", "decision", "entity", "other"], default="other"
    )
    store_p.add_argument("--importance", type=float, default=0.5)

    search_p = sub.add_parser("search", help="Search memories")
    search_p.add_argument("query", help="Search query")
    search_p.add_argument("--limit", type=int, default=5)
    search_p.add_argument("--agent", help="Filter by agent")
    search_p.add_argument("--json", action="store_true")

    forget_p = sub.add_parser("forget", help="Delete a memory")
    forget_p.add_argument("id_or_query", help="Memory ID or search query to find and delete")

    stats_p = sub.add_parser("stats", help="Show memory stats")
    stats_p.add_argument("--agent", help="Filter by agent")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    {"store": cmd_store, "search": cmd_search, "forget": cmd_forget, "stats": cmd_stats}[args.command](args)


if __name__ == "__main__":
    main()
