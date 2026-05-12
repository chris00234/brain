#!/opt/homebrew/bin/python3
"""Weekly memory nudge — Jenna reviews recent memories for promotion/archival.

Classifies memories as durable/obsolete/pattern, then takes real action:
- durable  → mark metadata.promotion_candidate=true (for canonical_pipeline to pick up)
- obsolete → mark memory_class=obsolete (hidden from default search)
- pattern  → store as new semantic_memory with category=preference
"""

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cli_llm import dispatch_with_schema  # migrated 2026-04-17
from vector_store import get_vector_store

OUT_FILE = Path("/Users/chrischo/server/brain/logs/memory-nudge-latest.json")
DISPATCH_STATE = Path("/Users/chrischo/server/brain/logs/memory-nudge-state.json")
DISPATCH_COOLDOWN_HOURS = 22  # ~daily

BRAIN_URL = "http://127.0.0.1:8791"

try:
    from config import SECRET_FILE, load_bearer_secret
except ImportError:
    SECRET_FILE = Path("/Users/chrischo/.openclaw/credentials/.personal_webhook_secret")

    def load_bearer_secret() -> str:
        return SECRET_FILE.read_text().strip()


PROMPT_TEMPLATE = """Review these recent memories from Chris's brain. For each, classify as:
- durable: should be promoted to canonical knowledge
- obsolete: no longer useful, archive
- pattern: reveals a reusable rule or behavior

Memories:
{memories}
"""

SCHEMA = '{"durable": [<memory_id>, ...], "obsolete": [<memory_id>, ...], "patterns": [{"rule": "...", "from": [<memory_id>, ...]}]}'


def fetch_recent(days: int = 7) -> list[dict]:
    points = get_vector_store().get(
        "semantic_memory",
        limit=200,
        with_payload=True,
        with_documents=True,
    )
    if not points:
        return []
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    recent = []
    for p in points:
        m = p.payload or {}
        if (m.get("memory_class") or "") == "obsolete":
            continue  # skip already-obsolete
        ts = m.get("created_at", "")
        if ts >= cutoff:
            recent.append(
                {
                    "id": p.id,
                    "content": (p.document or "")[:200],
                    "category": m.get("category", "other"),
                }
            )
    return recent[:50]  # cap for prompt size


def mark_obsolete(sem_id: str, memory_ids: list[str]) -> int:
    """Mark memories as obsolete. ``sem_id`` is accepted for back-compat
    (callers used to pass a ChromaDB UUID); ignored under VectorStore."""
    del sem_id  # unused — collection is addressed by name
    if not memory_ids:
        return 0
    store = get_vector_store()
    try:
        for mid in memory_ids:
            store.update_payload(
                "semantic_memory", ids=[mid], patch={"memory_class": "obsolete"}
            )
        return len(memory_ids)
    except Exception as e:
        print(f"mark_obsolete failed: {e}")
        return 0


def mark_promotion_candidate(sem_id: str, memory_ids: list[str]) -> int:
    del sem_id  # unused — see mark_obsolete
    if not memory_ids:
        return 0
    store = get_vector_store()
    now_iso = datetime.now(UTC).isoformat()
    try:
        for mid in memory_ids:
            store.update_payload(
                "semantic_memory",
                ids=[mid],
                patch={"promotion_candidate": "true", "promotion_flagged_at": now_iso},
            )
        return len(memory_ids)
    except Exception as e:
        print(f"mark_promotion_candidate failed: {e}")
        return 0


def store_patterns(patterns: list[dict]) -> int:
    """Store extracted behavioral patterns as new preference memories.
    Uses the brain's /memory endpoint so embedding + dedup flow through the
    same path as any other memory write.
    """
    if not patterns or not SECRET_FILE.exists():
        return 0
    try:
        secret = load_bearer_secret()
    except Exception:
        return 0
    from http_pool import http_json

    stored = 0
    for p in patterns:
        rule = (p.get("rule") or "").strip()
        if not rule or len(rule) < 10:
            continue
        from_ids = p.get("from") or []
        reason = (
            f"Derived from memories: {', '.join(str(i) for i in from_ids[:5])}"
            if from_ids
            else "Pattern extracted by memory_nudge"
        )
        payload = {
            "content": rule[:500],
            "category": "preference",
            "agent": "jenna",
            "source": "memory_nudge_pattern",
            "reason": reason[:200],
        }
        try:
            http_json(
                "POST",
                f"{BRAIN_URL}/memory",
                payload=payload,
                timeout=15,
                headers={"Authorization": f"Bearer {secret}"},
            )
            stored += 1
        except Exception as e:
            print(f"store_patterns [{rule[:40]}...] failed: {e}")
    return stored


def main():
    recent = fetch_recent(7)
    if not recent:
        print("No recent memories to review")
        return 0

    # Collection name is constant under QdrantStore; downstream callers
    # (mark_obsolete / mark_promotion_candidate) accept but don't use it.
    sem_id = "semantic_memory"

    # 2026-04-17 token-spike fix: cap per-memory content at 500 chars + cap
    # total memory count to 80. Prior unbounded concat of 7 days of memories
    # was a candidate source of the 760K-token Jenna prompts that blew the
    # weekly cache budget with 2-5% cache hit rate.
    _MAX_MEMS = 80
    _MAX_CHARS_PER_MEM = 500
    _trimmed = recent[:_MAX_MEMS]
    memory_text = "\n".join(
        f"- [{m['id']}] ({m['category']}) {(m['content'] or '')[:_MAX_CHARS_PER_MEM]}" for m in _trimmed
    )
    prompt = PROMPT_TEMPLATE.format(memories=memory_text)

    parsed = dispatch_with_schema(
        agent="jenna",
        message=prompt,
        schema_description=SCHEMA,
        thinking="low",
        timeout=120,
        max_retries=1,
    )
    if parsed is None:
        print("Dispatch failed or JSON parse failed after retries")
        return 1

    # Validate recent IDs exist — avoid marking IDs that don't belong to the batch
    recent_id_set = {m["id"] for m in recent}
    durable_ids = [i for i in parsed.get("durable", []) if isinstance(i, str) and i in recent_id_set]
    obsolete_ids = [i for i in parsed.get("obsolete", []) if isinstance(i, str) and i in recent_id_set]
    patterns = parsed.get("patterns", []) or []

    # Act on classifications
    promoted_count = mark_promotion_candidate(sem_id, durable_ids)
    archived_count = mark_obsolete(sem_id, obsolete_ids)
    patterns_stored = store_patterns(patterns)

    report = {
        "timestamp": datetime.now().isoformat(),
        "reviewed_count": len(recent),
        "durable": durable_ids,
        "obsolete": obsolete_ids,
        "patterns": patterns,
        "actions": {
            "promotion_flagged": promoted_count,
            "archived": archived_count,
            "patterns_stored": patterns_stored,
        },
    }
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(report, indent=2))

    print(f"Reviewed {len(recent)} memories")
    print(f"  durable (promotion flagged): {promoted_count}")
    print(f"  obsolete (archived): {archived_count}")
    print(f"  patterns: {len(patterns)}")

    # Telegram dispatch — fire only when there's something material AND we
    # haven't dispatched in the last cooldown window. Caps spam to ~1/day.
    severity = "high" if (promoted_count >= 5 or archived_count >= 10 or len(patterns) >= 3) else "low"
    if severity == "high":
        try:
            last = {}
            if DISPATCH_STATE.exists():
                last = json.loads(DISPATCH_STATE.read_text())
            last_iso = last.get("last_dispatched_at", "")
            should_dispatch = True
            if last_iso:
                try:
                    last_dt = datetime.fromisoformat(last_iso.replace("Z", "+00:00"))
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=UTC)
                    age_hours = (datetime.now(UTC) - last_dt).total_seconds() / 3600
                    should_dispatch = age_hours >= DISPATCH_COOLDOWN_HOURS
                except Exception:
                    pass
            if should_dispatch:
                # 2026-04-17: was `cli_llm.dispatch(agent="jenna", ...)` which
                # ignores `agent` and just runs codex exec, throwing the
                # response away — Chris never got the Telegram nudge. Now
                # goes through the unified direct-Bot-API alert path.
                from telegram_alert import send_chris_telegram

                msg = (
                    f"[BRAIN MEMORY NUDGE] Reviewed {len(recent)} recent memories.\n"
                    f"  Promoted to canonical-candidate: {promoted_count}\n"
                    f"  Marked obsolete: {archived_count}\n"
                    f"  Patterns extracted: {len(patterns)}\n"
                    f"Review at https://brain.chrischodev.com/memory"
                )
                send_chris_telegram(msg, source="memory_nudge", severity="info")
                DISPATCH_STATE.parent.mkdir(parents=True, exist_ok=True)
                DISPATCH_STATE.write_text(
                    json.dumps(
                        {
                            "last_dispatched_at": datetime.now(UTC).isoformat(),
                            "promoted": promoted_count,
                            "archived": archived_count,
                            "patterns": len(patterns),
                        },
                        indent=2,
                    )
                )
                print("  → dispatched memory nudge to Jenna (Telegram)")
            else:
                print("  → dispatch skipped (cooldown active)")
        except Exception as e:
            print(f"  → dispatch failed: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
