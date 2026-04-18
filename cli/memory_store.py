#!/opt/homebrew/bin/python3
"""Semantic memory store — persistent ChromaDB replacement for Qdrant in-memory.

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

COLLECTION = "semantic_memory"
DUPLICATE_THRESHOLD = 0.05


def chroma_api(method, path, data=None):
    """Call ChromaDB API on native instance (127.0.0.1:8000)."""
    import urllib.request

    url = f"http://127.0.0.1:8000{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(
        url, data=body, method=method, headers={"Content-Type": "application/json"} if body else {}
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read()
    return json.loads(raw) if raw.strip() else {}


def get_collection_id(auto_create=True):
    cols = chroma_api("GET", "/api/v2/tenants/default_tenant/databases/default_database/collections")
    for c in cols:
        if c["name"] == COLLECTION:
            return c["id"]
    if auto_create:
        result = chroma_api(
            "POST",
            "/api/v2/tenants/default_tenant/databases/default_database/collections",
            {"name": COLLECTION, "metadata": {"hnsw:space": "cosine"}},
        )
        return result.get("id")
    return None


def cmd_store(args):
    col_id = get_collection_id()
    if not col_id:
        print(f"Failed to access or create collection '{COLLECTION}'. Check ChromaDB connectivity.")
        sys.exit(1)

    text = args.text
    agent = args.agent or "unknown"
    category = args.category or "other"
    importance = args.importance or 0.5

    embedding = get_embedding(text)

    search_result = chroma_api(
        "POST",
        f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/query",
        {"query_embeddings": [embedding], "n_results": 1, "include": ["distances"]},
    )
    dists = search_result.get("distances", [[]])[0]
    if dists and dists[0] < DUPLICATE_THRESHOLD:
        print(f"Duplicate detected (distance: {dists[0]:.4f}). Skipping.")
        return

    doc_id = f"mem:{agent}:{hashlib.md5(text.encode()).hexdigest()}"[:63]
    now = datetime.now().isoformat()

    chroma_api(
        "POST",
        f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/upsert",
        {
            "ids": [doc_id],
            "embeddings": [embedding],
            "documents": [text],
            "metadatas": [
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
        },
    )
    print(f"Stored: {text[:80]}... [agent={agent}, category={category}]")


def cmd_search(args):
    col_id = get_collection_id()
    if not col_id:
        print(f"Collection '{COLLECTION}' not found.")
        sys.exit(1)

    embedding = get_embedding(args.query, prefix="query")
    n = args.limit or 5

    payload = {
        "query_embeddings": [embedding],
        "n_results": n,
        "include": ["documents", "metadatas", "distances"],
    }

    if args.agent:
        payload["where"] = {"agent": {"$eq": args.agent}}

    result = chroma_api(
        "POST",
        f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/query",
        payload,
    )

    docs = result.get("documents", [[]])[0]
    metas = result.get("metadatas", [[]])[0]
    dists = result.get("distances", [[]])[0]

    results = []
    for i in range(len(docs)):
        results.append(
            {
                "content": docs[i],
                "score": round(1 - dists[i], 4),
                "agent": metas[i].get("agent", ""),
                "category": metas[i].get("category", ""),
                "created_at": metas[i].get("created_at", ""),
                "source": metas[i].get("source", ""),
            }
        )

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        if not results:
            print("No memories found.")
            return
        for i, r in enumerate(results):
            print(f"#{i+1} (score: {r['score']:.3f}) [{r['category']}] {r['agent']}")
            print(f"  {r['content'][:200]}")
            print()


def cmd_forget(args):
    col_id = get_collection_id()
    if not col_id:
        print(f"Collection '{COLLECTION}' not found.")
        sys.exit(1)

    target = args.id_or_query

    if target.startswith("mem:"):
        chroma_api(
            "POST",
            f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/delete",
            {"ids": [target]},
        )
        print(f"Deleted: {target}")
        return

    embedding = get_embedding(target)
    result = chroma_api(
        "POST",
        f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/query",
        {"query_embeddings": [embedding], "n_results": 1, "include": ["documents", "distances"]},
    )

    dists = result.get("distances", [[]])[0]
    ids = result.get("ids", [[]])[0]
    docs = result.get("documents", [[]])[0]

    if not ids or dists[0] > 0.3:
        print(f"No close match found for: {target}")
        return

    chroma_api(
        "POST",
        f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/delete",
        {"ids": [ids[0]]},
    )
    print(f"Deleted: {ids[0]} — {docs[0][:80]}...")


def cmd_stats(args):
    col_id = get_collection_id()
    if not col_id:
        print(f"Collection '{COLLECTION}' not found.")
        sys.exit(1)

    if args.agent:
        payload = {
            "where": {"agent": {"$eq": args.agent}},
            "include": [],
            "limit": 10000,
        }
        result = chroma_api(
            "POST",
            f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/get",
            payload,
        )
        ids = result.get("ids", [])
        print(f"Semantic memories (agent={args.agent}): {len(ids)}")
    else:
        count_result = chroma_api(
            "GET", f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/count"
        )
        total = count_result if isinstance(count_result, int) else count_result.get("count", "unknown")
        print(f"Semantic memories: {total}")


def main():
    parser = argparse.ArgumentParser(description="Semantic Memory Store (ChromaDB)")
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
