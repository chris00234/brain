#!/opt/homebrew/bin/python3
"""brain_core/pipeline/event_compressor.py — Monthly event compression.

Finds events >90 days old in the `experience` collection, groups them by
calendar month, dispatches each batch to Jenna for summarization, then writes
the summaries back to a dedicated `experience_compressed` collection. Original
events are marked memory_class="obsolete" (kept for provenance) with a pointer
to the compressed digest.

Runs monthly (1st of month, 4:00am).
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cli_llm import dispatch_with_schema  # migrated 2026-04-17
from vector_store import get_vector_store

MIN_EVENTS_PER_MONTH = 5
MAX_EVENTS_PER_PROMPT = 100
UPDATE_BATCH_SIZE = 100
CUTOFF_DAYS = 90

SCHEMA = '{"summary": "1-2 paragraph summary", "key_events": ["event 1", "event 2"], "incidents": []}'

PROMPT = """Summarize these events from {month} into a concise monthly digest.
Identify key decisions, incidents, and patterns.

Events:
{events}

Return strict JSON matching the schema."""


def _get_or_create_compressed_collection() -> str:
    """Ensure experience_compressed exists, return its name."""
    get_vector_store().create_collection(
        "experience_compressed", {"source": "event_compressor"}
    )
    return "experience_compressed"


def compress_month(month: str, events: list[dict]) -> str | None:
    """Compress a batch of events for a given month. Returns digest text or None."""
    events_text = "\n".join(f"- {e.get('content', '')[:200]}" for e in events[:MAX_EVENTS_PER_PROMPT])
    parsed = dispatch_with_schema(
        agent="jenna",
        message=PROMPT.format(month=month, events=events_text),
        schema_description=SCHEMA,
        thinking="low",
        timeout=180,
        max_retries=1,
    )
    if not parsed:
        return None
    summary = parsed.get("summary", "")
    key_events = parsed.get("key_events", []) or []
    incidents = parsed.get("incidents", []) or []

    full_text = f"# Monthly digest: {month}\n\n{summary}\n\n## Key events\n"
    full_text += "\n".join(f"- {e}" for e in key_events)
    if incidents:
        full_text += "\n\n## Incidents\n" + "\n".join(f"- {i}" for i in incidents)
    return full_text


def main() -> int:
    from _watchdog import arm as _arm_watchdog

    _arm_watchdog(600, tag="event_compressor")
    store = get_vector_store()
    cutoff_date = (datetime.now(UTC) - timedelta(days=CUTOFF_DAYS)).isoformat()

    # Single-call full scan via native cursor.
    try:
        points = store.get(
            "experience",
            limit=1_000_000,
            with_payload=True,
            with_documents=True,
        )
    except Exception as e:
        print(f"fetch from experience failed: {e}")
        return 1
    ids: list[str] = [p.id for p in points]
    docs: list[str] = [(p.document or "") for p in points]
    metas: list[dict] = [(p.payload or {}) for p in points]

    by_month: dict[str, list[dict]] = defaultdict(list)
    to_compress_ids: list[str] = []

    for mid, doc, meta in zip(ids, docs, metas, strict=False):
        meta = meta or {}
        created = meta.get("created_at", "")
        if not created or created >= cutoff_date:
            continue
        if meta.get("memory_class") in ("compressed", "obsolete"):
            continue  # already compressed or previously demoted
        month = created[:7]  # YYYY-MM
        by_month[month].append({"id": mid, "content": doc or "", "metadata": meta})
        to_compress_ids.append(mid)

    if not by_month:
        print("No events to compress")
        return 0

    print(f"Found {len(to_compress_ids)} events across {len(by_month)} months to compress")

    comp_col = _get_or_create_compressed_collection()

    # Lazy import to avoid pulling indexer on module load
    from indexer import get_embedding

    compressed_months = 0
    skipped_months = 0
    for month, events in sorted(by_month.items()):
        if len(events) < MIN_EVENTS_PER_MONTH:
            skipped_months += 1
            continue

        summary = compress_month(month, events)
        if not summary:
            print(f"  {month}: compression failed")
            continue

        emb = get_embedding(summary[:1000], prefix="passage")
        if not emb:
            print(f"  {month}: embedding failed")
            continue

        try:
            store.upsert(
                comp_col,
                ids=[f"compressed:{month}"],
                vectors=[emb],
                documents=[summary],
                payloads=[
                    {
                        "month": month,
                        "memory_class": "compressed",
                        "event_count": len(events),
                        "created_at": datetime.now(UTC).isoformat(),
                    }
                ],
            )
            compressed_months += 1
            print(f"  {month}: compressed {len(events)} events")
        except Exception as e:
            print(f"  {month}: upsert failed: {e}")
            continue

        # Mark originals as obsolete
        patch = {
            "memory_class": "obsolete",
            "compressed_into": f"compressed:{month}",
        }
        for e in events:
            try:
                store.update_payload("experience", ids=[e["id"]], patch=patch)
            except Exception as exc:
                print(f"  {month}: update failed for {e['id']}: {exc}")

    print(f"\nCompressed {compressed_months} months, skipped {skipped_months} (too few events)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
