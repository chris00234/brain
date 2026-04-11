#!/opt/homebrew/bin/python3
"""Training pair generator — builds contrastive pairs from search-feedback.jsonl.

Reads user feedback on search results and generates positive/negative pairs
for LoRA fine-tuning of the embedding model.
"""
from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from http_pool import http_json
from search import get_collections


FEEDBACK_LOG = Path("/Users/chrischo/server/brain/logs/search-feedback.jsonl")
TRAINING_DIR = Path("/Users/chrischo/server/brain/logs/training")

CHROMA_URL = "http://127.0.0.1:8000"
CHROMA_API = f"{CHROMA_URL}/api/v2/tenants/default_tenant/databases/default_database/collections"

MIN_POSITIVE_PAIRS = 100  # minimum to be worth training
HARD_NEGATIVES_PER_POSITIVE = 3


def read_feedback_events(since_days: int = 30) -> list[dict]:
    """Read feedback events from the last N days."""
    if not FEEDBACK_LOG.exists():
        return []
    events: list[dict] = []
    with FEEDBACK_LOG.open() as f:
        for line in f:
            try:
                entry = json.loads(line)
                events.append(entry)
            except Exception:
                continue
    cutoff = datetime.now() - timedelta(days=since_days)
    filtered: list[dict] = []
    for e in events:
        ts = e.get("timestamp", "")
        if not ts:
            continue
        try:
            entry_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if entry_dt.tzinfo is not None:
                entry_dt = entry_dt.replace(tzinfo=None)
        except Exception:
            continue
        if entry_dt >= cutoff:
            filtered.append(e)
    return filtered


def fetch_memory_content(result_id: str, source: str) -> str | None:
    """Fetch the actual document content for a given result_id."""
    try:
        cols = get_collections()
        col_name: str | None = None
        if ":" in result_id:
            prefix, _ = result_id.split(":", 1)
            if prefix in cols:
                col_name = prefix
        if not col_name and source:
            col_name = source if source in cols else None
        if not col_name:
            return None

        col_id = cols[col_name]
        resp = http_json(
            "POST",
            f"{CHROMA_API}/{col_id}/get",
            {"ids": [result_id], "include": ["documents"]},
        )
        docs = resp.get("documents", []) or []
        if docs and docs[0]:
            return docs[0][:1000]
    except Exception:
        pass
    return None


def sample_hard_negatives(query: str, positive_content: str, count: int = 3) -> list[str]:
    """Sample random unrelated documents as hard negatives.

    Strategy: pull random documents from knowledge/semantic_memory collections
    that share NO significant tokens with the query.
    """
    try:
        cols = get_collections()
        col_id = cols.get("knowledge") or cols.get("semantic_memory")
        if not col_id:
            return []

        resp = http_json(
            "POST",
            f"{CHROMA_API}/{col_id}/get",
            {"limit": 100, "include": ["documents"]},
        )
        docs = resp.get("documents", []) or []
        if not docs:
            return []

        query_tokens = {w.lower() for w in query.split() if len(w) > 3}
        candidates: list[str] = []
        for doc in docs:
            if not doc:
                continue
            doc_tokens = {w.lower() for w in doc.split()[:50] if len(w) > 3}
            overlap = len(query_tokens & doc_tokens)
            if overlap == 0 and doc != positive_content:
                candidates.append(doc[:500])

        random.shuffle(candidates)
        return candidates[:count]
    except Exception:
        return []


def generate_pairs(since_days: int = 30) -> dict:
    """Generate training pairs from recent feedback."""
    events = read_feedback_events(since_days=since_days)
    if not events:
        return {"status": "no_events", "pairs": 0}

    positive_pairs: list[dict] = []
    negative_pairs: list[dict] = []

    for event in events:
        query = (event.get("query") or "").strip()
        result_id = (event.get("result_id") or "").strip()
        source = (event.get("source") or "").strip()
        useful = bool(event.get("useful", False))
        agent = event.get("agent") or "system"

        if not query or not result_id:
            continue

        content = fetch_memory_content(result_id, source)
        if not content:
            continue

        if useful:
            hard_negs = sample_hard_negatives(query, content, count=HARD_NEGATIVES_PER_POSITIVE)
            pair = {
                "query": query,
                "positive": content,
                "negatives": hard_negs,
                "label": "useful",
                "source": source,
                "agent": agent,
                "timestamp": event.get("timestamp", ""),
            }
            positive_pairs.append(pair)
        else:
            pair = {
                "query": query,
                "negative_direct": content,
                "label": "not_useful",
                "source": source,
                "agent": agent,
                "timestamp": event.get("timestamp", ""),
            }
            negative_pairs.append(pair)

    TRAINING_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y_%m_%d")
    out_file = TRAINING_DIR / f"pairs_{today}.jsonl"

    with out_file.open("w") as f:
        for pair in positive_pairs + negative_pairs:
            f.write(json.dumps(pair, ensure_ascii=False) + "\n")

    return {
        "status": "ok",
        "positive_pairs": len(positive_pairs),
        "negative_pairs": len(negative_pairs),
        "total": len(positive_pairs) + len(negative_pairs),
        "output_file": str(out_file),
        "ready_for_training": len(positive_pairs) >= MIN_POSITIVE_PAIRS,
        "minimum_needed": MIN_POSITIVE_PAIRS,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Generate training pairs from feedback")
    parser.add_argument("--since-days", type=int, default=30)
    args = parser.parse_args()

    result = generate_pairs(since_days=args.since_days)
    print(json.dumps(result, indent=2))
    sys.exit(0)
