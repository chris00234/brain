#!/opt/homebrew/bin/python3
"""Record a learning/experience to the vector store. Called by agents after tasks."""

import hashlib
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
from search import get_embedding
from vector_store import get_vector_store


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

    store = get_vector_store()
    if collection not in store.list_collections():
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
    store.upsert(
        collection,
        ids=[doc_id],
        vectors=[embedding],
        documents=[content],
        payloads=[metadata],
    )
    print(f"Recorded to [{collection}] by {agent}: {content[:80]}...")
