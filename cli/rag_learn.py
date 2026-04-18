#!/opt/homebrew/bin/python3
"""Record a learning/experience to RAG. Called by agents after tasks."""

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
from search import get_embedding


def get_collection_id(name):
    """Get ChromaDB collection ID by name."""
    import urllib.request

    url = f"http://127.0.0.1:8000/api/v2/tenants/default_tenant/databases/default_database/collections/{name}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    return data.get("id")


def upsert(col_id, ids, embeddings, documents, metadatas):
    """Upsert documents into ChromaDB collection."""
    import urllib.request

    url = f"http://127.0.0.1:8000/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/upsert"
    payload = json.dumps(
        {"ids": ids, "embeddings": embeddings, "documents": documents, "metadatas": metadatas}
    ).encode()
    req = urllib.request.Request(
        url, data=payload, method="POST", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: rag_learn.py <collection> <agent> <content> [type] [service] [tags]")
        print("  collection: knowledge | experience | context")
        print("  agent: ellie | liz | jenna | sage | market")
        print("  content: text to record")
        print("  type: config | error | decision | qa | learning (default: learning)")
        print("  service: ghost | nginx | ... (default: empty)")
        print("  tags: comma-separated (default: empty)")
        sys.exit(1)

    collection = sys.argv[1]
    agent = sys.argv[2]
    content = sys.argv[3]
    doc_type = sys.argv[4] if len(sys.argv) > 4 else "learning"
    service = sys.argv[5] if len(sys.argv) > 5 else ""
    tags = sys.argv[6] if len(sys.argv) > 6 else ""

    col_id = get_collection_id(collection)
    if not col_id:
        print(f"Collection '{collection}' not found")
        sys.exit(1)

    doc_id = f"learn:{agent}:{hashlib.md5(content.encode()).hexdigest()}"[:63]
    now = datetime.now().isoformat()

    metadata = {
        "source": f"rag_learn:{agent}",
        "agent": agent,
        "type": doc_type,
        "service": service,
        "section": "",
        "tags": tags,
        "created_at": now,
    }

    embedding = get_embedding(content)
    upsert(col_id, [doc_id], [embedding], [content], [metadata])
    print(f"Recorded to [{collection}] by {agent}: {content[:80]}...")
