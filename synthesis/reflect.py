#!/opt/homebrew/bin/python3
"""brain_reflect — nightly Sage-driven reflection on recent self-learning captures.

Runs at 02:45 daily via ai.openclaw.brain-reflect.plist. Pulls semantic_memory
entries from the last 7 days, dispatches to Sage (research agent) via OpenClaw
for pattern + contradiction detection, writes findings to raw/inbox/ as
schema-compliant raw records. The existing canonical pipeline (pipeline_auto.py)
picks them up on its next run and promotes durable insights to canonical.

Constraints honored:
- Only LLM call is via `openclaw agent --agent sage` (existing OpenAI subscription).
- Ollama untouched (embedder-only).
- No new daemons; this is a one-shot launchd job.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, "/Users/chrischo/server/brain/brain_core")
from cli_llm import dispatch_with_schema  # migrated 2026-04-17
from vector_store import get_vector_store
from safe_state import atomic_write_text

SEMANTIC_COLLECTION = "semantic_memory"
INBOX_DIR = Path("/Users/chrischo/server/knowledge/raw/inbox")
LOG_FILE = Path("/Users/chrischo/.openclaw/logs/brain-reflect.log")
LOOKBACK_DAYS = 7
DISPATCH_TIMEOUT = 240

REFLECT_SCHEMA = """{
  "patterns": [{"description": "<observed pattern>", "evidence_ids": [...], "confidence": 0.0}],
  "contradictions": [{"a_id": "<id>", "b_id": "<id>", "explanation": "<A vs B, which is current>"}],
  "missing_context": ["<one-line gap>"]
}"""


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _log(msg: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a") as f:
        f.write(f"{datetime.now().isoformat()} {msg}\n")


def fetch_recent_memories() -> tuple[list[dict], list[dict]]:
    """Return (new_memories, all_preferences) for contradiction detection."""
    try:
        points = get_vector_store().get(
            SEMANTIC_COLLECTION,
            limit=500,
            with_payload=True,
            with_documents=True,
        )
    except Exception as e:
        _log(f"ERROR chroma get: {e}")
        return [], []
    if not points:
        _log("ERROR semantic_memory collection empty or missing")
        return [], []

    cutoff = datetime.now(UTC) - timedelta(days=LOOKBACK_DAYS)

    new_memories: list[dict] = []
    all_preferences: list[dict] = []

    for p in points:
        mem_id = p.id
        doc = p.document or ""
        meta = p.payload or {}
        ts_raw = meta.get("created_at") or meta.get("updated_at") or ""
        category = meta.get("category", "other")
        superseded = meta.get("superseded_by", "")

        entry = {
            "id": mem_id,
            "content": doc or "",
            "category": category,
            "agent": meta.get("agent", "unknown"),
            "source": meta.get("source", "unknown"),
            "created_at": ts_raw,
        }

        # All active (non-superseded) preferences — no date filter
        if category == "preference" and not superseded:
            all_preferences.append(entry)

        # Recent memories — date-filtered
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if ts >= cutoff:
            new_memories.append(entry)

    return new_memories, all_preferences


REFLECT_PROMPT = """You are Sage. Review these recent learnings about Chris and compare them against all existing preferences.

Your job: find CONTRADICTIONS between NEW memories and EXISTING preferences. The most common type: Chris stated a preference months ago, and a new memory shows he changed his mind.

Examples:
- Old preference says "Chris prefers React" but new memory says "Chris switched to Svelte"
- Old preference says "deploy via docker compose" but new memory says "moved to native macOS"
- Old preference says "Ollama runs nomic-embed-text" but new memory says "switched to multilingual-e5"
- Same preference stated differently at different times (which is current?)

Also find patterns (recurring themes in new memories) and gaps (missing context).

Return JSON ONLY:

{{
  "patterns": [
    {{"description": "<observed pattern>", "evidence_ids": [...], "confidence": 0.0-1.0}}
  ],
  "contradictions": [
    {{"a_id": "<id>", "b_id": "<id>", "explanation": "<what A says vs what B says, and which is likely current>"}}
  ],
  "missing_context": [
    "<one-line gap that should be probed>"
  ]
}}

Maximum 5 entries per array. Be aggressive about finding contradictions — stale preferences that were updated are the most common type. No prose outside JSON. No markdown fences.

NEW MEMORIES (last 7 days, {new_count} total):
{new_memories}

ALL ACTIVE PREFERENCES ({pref_count} total):
{all_preferences}

JSON:"""


# 2026-04-16 Tier 3 #12: Chain-of-Note (Yu et al. 2023) — sequential pass
# that maintains a running belief state. Reduces single-shot false-positive
# contradictions by requiring temporal consistency before flagging.
CHAIN_OF_NOTE_PROMPT = """You are Sage reviewing Chris's memories in chronological order. Maintain a running belief state as you read each memory, then report only contradictions that are supported across multiple observations (not single outliers).

METHOD:
1. Read memories chronologically (they're pre-sorted below).
2. For each new memory, maintain internal belief state: what Chris currently prefers / what is true.
3. Flag a contradiction ONLY when: (a) a new memory contradicts an entrenched earlier preference AND (b) no later memory reverts the change. Single transient mentions don't count.
4. Patterns: report only when you see the same theme in 3+ memories.

This reduces false positives — a one-off mention of "maybe I should try X" is not a real preference flip.

Return JSON ONLY:

{{
  "patterns": [
    {{"description": "<observed pattern>", "evidence_ids": [...], "confidence": 0.0-1.0, "occurrences": <int>}}
  ],
  "contradictions": [
    {{"a_id": "<id>", "b_id": "<id>", "explanation": "<what A says vs what B says, and which is likely current>", "corroboration_count": <int>}}
  ],
  "missing_context": [
    "<one-line gap that should be probed>"
  ]
}}

Drop any contradiction with corroboration_count < 2. Maximum 5 per array. No prose outside JSON. No markdown fences.

NEW MEMORIES IN CHRONOLOGICAL ORDER (last 7 days, {new_count} total):
{new_memories}

ALL ACTIVE PREFERENCES ({pref_count} total):
{all_preferences}

JSON:"""


def dispatch_to_sage(new_memories: list[dict], all_preferences: list[dict]) -> dict | None:
    if not new_memories:
        return None
    # 2026-04-16 Tier 3 #12: Chain-of-Note. Sort memories chronologically
    # so Sage can maintain a running belief state as it reads them. The
    # prompt enforces corroboration_count >= 2 and occurrences >= 3, so
    # single outlier mentions no longer trigger false contradictions.
    sorted_new = sorted(new_memories, key=lambda m: m.get("created_at", ""))
    fmt = (
        lambda m: f"- [{m['id'][:24]}] ({m['category']}, {m['agent']}, {m['created_at'][:10]}) {m['content'][:200]}"
    )
    new_fmt = "\n".join(fmt(m) for m in sorted_new[:60])
    pref_fmt = "\n".join(fmt(m) for m in all_preferences[:500]) or "(none)"
    prompt = CHAIN_OF_NOTE_PROMPT.format(
        new_count=len(sorted_new),
        new_memories=new_fmt,
        pref_count=len(all_preferences),
        all_preferences=pref_fmt,
    )

    parsed = dispatch_with_schema(
        agent="sage",
        message=prompt,
        schema_description=REFLECT_SCHEMA,
        thinking="medium",
        timeout=DISPATCH_TIMEOUT,
        max_retries=1,
        backlog_kind="reflect",
        backlog_payload={
            "agent": "sage",
            "prompt": prompt,
            "thinking": "medium",
            "timeout": DISPATCH_TIMEOUT,
            "source": "brain_reflect",
        },
    )
    if parsed is None:
        _log("ERROR dispatch_with_schema returned None")
    return parsed


def index_patterns(reflection: dict) -> int:
    """Index extracted patterns into the knowledge collection with origin=patterns.

    Qdrant migration merged the old `patterns` collection into `knowledge`
    via payload discriminator (``origin="patterns"``). Writing to a bare
    `patterns` name creates an orphan collection with no HNSW tuning or
    sparse index, so route through the discriminator instead.
    """
    patterns = reflection.get("patterns", [])
    if not patterns:
        return 0

    docs = []
    for p in patterns:
        if isinstance(p, dict):
            desc = p.get("description", "")
        else:
            desc = str(p)
        if len(desc) < 20:
            continue
        docs.append(
            {
                "content": desc,
                "source": "brain-reflect:nightly",
                "type": "pattern",
                "origin": "patterns",
                "service": "",
                "agent": "sage",
                "section": "",
            }
        )

    if not docs:
        return 0

    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))
        from indexer import add_documents

        count = add_documents("knowledge", docs, skip_stale_cleanup=True)
        _log(f"indexed {count} patterns into knowledge collection (origin=patterns)")
        return count
    except Exception as e:
        _log(f"ERROR indexing patterns: {e}")
        return 0


def write_to_inbox(reflection: dict, memory_count: int) -> Path | None:
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    iso = _now_iso()
    payload = {
        "reflection": reflection,
        "memory_window_days": LOOKBACK_DAYS,
        "memory_count": memory_count,
        "generated_by": "brain_reflect",
    }
    ref_data = reflection or {}
    content_parts = [
        f"Nightly Reflection ({payload.get('memory_window_days', 7)}-day window, {payload.get('memory_count', 0)} memories)\n"
    ]
    patterns = ref_data.get("patterns", [])
    if patterns:
        content_parts.append("## Patterns Observed")
        for p in patterns:
            desc = p.get("description", p) if isinstance(p, dict) else str(p)
            conf = (
                f" (confidence: {p.get('confidence', '?')})"
                if isinstance(p, dict) and "confidence" in p
                else ""
            )
            content_parts.append(f"- {desc}{conf}")
    contradictions = ref_data.get("contradictions", [])
    if contradictions:
        content_parts.append("\n## Contradictions")
        for c in contradictions:
            expl = c.get("explanation", c) if isinstance(c, dict) else str(c)
            content_parts.append(f"- {expl}")
    missing = ref_data.get("missing_context", [])
    if missing:
        content_parts.append("\n## Missing Context")
        for m in missing:
            content_parts.append(f"- {m}")
    content_str = "\n".join(content_parts)
    digest = hashlib.sha256(content_str.encode()).hexdigest()
    rec_id = f"raw_reflection_{iso[:10].replace('-', '_')}_{digest[:8]}"

    record = {
        "id": rec_id,
        "timestamp": iso,
        "source_type": "reflection",
        "source_ref": "brain-reflect:nightly",
        "actor": "sage",
        "visibility": "private",
        "scrub_status": "scrubbed",
        "content": content_str,
        "attachments": [],
        "entities": ["Chris"],
        "hash": f"sha256:{digest}",
    }
    out = INBOX_DIR / f"{rec_id}.json"
    atomic_write_text(out, json.dumps(record, ensure_ascii=False, indent=2))
    return out


def main() -> int:
    _log("=== brain_reflect start ===")
    new_memories, all_preferences = fetch_recent_memories()
    _log(
        f"fetched {len(new_memories)} new memories from last {LOOKBACK_DAYS}d, {len(all_preferences)} active preferences"
    )
    if not new_memories:
        _log("nothing to reflect on, exiting")
        return 0

    reflection = dispatch_to_sage(new_memories, all_preferences)
    if not reflection:
        _log("dispatch failed, exiting")
        return 1

    out = write_to_inbox(reflection, len(new_memories))
    _log(f"wrote {out}")

    # Index patterns into ChromaDB patterns collection
    pattern_count = index_patterns(reflection)

    print(
        json.dumps(
            {
                "status": "ok",
                "out": str(out),
                "memories": len(new_memories),
                "patterns_indexed": pattern_count,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
