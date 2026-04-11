"""brain_core/learn.py — automatic self-learning extraction.

Pipeline (per session transcript):
  1. extract_candidates  — regex-based passage scoring
  2. distill_via_jenna   — single OpenClaw dispatch (Jenna, low thinking) returns JSON
  3. embed_and_store     — Ollama embed + write to semantic_memory collection
  4. check_contradictions — vector + heuristic contradiction detection

The only LLM call is step 2 (via the OpenClaw gateway → OpenAI subscription).
Ollama is used only for embeddings. No new LLM hosting, no extra spend.

Called by: brain_server.py POST /learn (BackgroundTask)
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("brain.learn")

# Reuse the indexer's ChromaDB + Ollama helpers (sibling in brain_core/).
sys.path.insert(0, str(Path(__file__).parent))
from indexer import (  # noqa: E402
    chroma_api,
    get_embedding,
    ensure_collection,
    _get_collection_id,
    EMBED_MODEL,
    EMBED_MODEL_VERSION,
)
from openclaw_dispatch import dispatch as _dispatch  # noqa: E402

try:
    from config import OPENCLAW_BIN
except ImportError:
    OPENCLAW_BIN = "/Users/chrischo/.local/bin/openclaw"
SEMANTIC_COLLECTION = "semantic_memory"
CONTRADICTIONS_COLLECTION = "semantic_contradictions"
MAX_PER_SESSION = 5  # matches CLAUDE.md self-learning protocol cap
SIMILARITY_THRESHOLD = 0.85  # cosine distance below this = potential contradiction
TOKEN_DIVERGENCE_THRESHOLD = 0.5  # token overlap below this = contradicts (semantically close, lexically different)
DISTILL_TIMEOUT_SEC = 90
EMBED_TRUNCATE = 1000

# ── Trigger heuristics ──────────────────────────────────────────────────
POSITIVE_TRIGGERS = re.compile(
    r"\b(good|great|perfect|nice|awesome|exactly|love it|brilliant|wonderful|"
    r"that.s right|works|excellent|love this|love that|best|amazing)\b",
    re.IGNORECASE,
)
NEGATIVE_TRIGGERS = re.compile(
    r"\b(don.t like|not what i wanted|that.s wrong|wrong|undo|instead of|"
    r"fix this|change|hate|bad|stop|never|don.t do that|why did you)\b",
    re.IGNORECASE,
)
PREFERENCE_DECLARATIONS = re.compile(
    r"\b(i prefer|i always|i never|i like|i hate|i want|i need|"
    r"my (?:approach|preference|rule|style|workflow|setup) is|"
    r"i.m the kind of|i tend to|i don.t want|i won.t)\b",
    re.IGNORECASE,
)
KOREAN_POSITIVE = re.compile(r"(좋아|좋네|완벽|잘했어|굿|좋다|짱|멋지다|최고)")
KOREAN_NEGATIVE = re.compile(r"(왜 그랬어|별로|아니야|다시|그게 아니라|싫어|별루)")
FACT_DECLARATIONS = re.compile(
    r"\b(my (?:name|job|role|location|wife|girlfriend|car|setup|team) is|"
    r"i live in|i work at|i.m a|i'm a|i was born|i graduated)\b",
    re.IGNORECASE,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_\-]{3,}", text.lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _cosine(v1: list[float], v2: list[float]) -> float:
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    dot = sum(x * y for x, y in zip(v1, v2))
    n1 = math.sqrt(sum(x * x for x in v1))
    n2 = math.sqrt(sum(y * y for y in v2))
    if n1 == 0 or n2 == 0:
        return 0.0
    return dot / (n1 * n2)


# ── Step 1: candidate extraction ────────────────────────────────────────
def extract_candidates(transcript: str) -> list[dict[str, Any]]:
    """Score paragraphs for learning-worthiness. Returns top N triggered passages.

    A passage is a non-empty line or short block. Each gets a score based on which
    trigger patterns it matches; the LLM later decides which to actually persist.
    """
    if not transcript:
        return []

    candidates: list[dict[str, Any]] = []
    blocks = [b.strip() for b in re.split(r"\n{2,}", transcript) if b.strip()]

    for idx, block in enumerate(blocks):
        if len(block) < 20 or len(block) > 2000:
            continue

        score = 0
        triggers: list[str] = []

        if POSITIVE_TRIGGERS.search(block):
            score += 1
            triggers.append("positive")
        if NEGATIVE_TRIGGERS.search(block):
            score += 2
            triggers.append("negative")
        if PREFERENCE_DECLARATIONS.search(block):
            score += 3
            triggers.append("preference")
        if FACT_DECLARATIONS.search(block):
            score += 3
            triggers.append("fact")
        if KOREAN_POSITIVE.search(block) or KOREAN_NEGATIVE.search(block):
            score += 2
            triggers.append("korean")

        if score > 0:
            candidates.append({
                "block": block,
                "score": score,
                "triggers": triggers,
                "position": idx,
            })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[: MAX_PER_SESSION * 2]  # send 2x to LLM, it filters


# ── Step 2: distill via OpenClaw Jenna ──────────────────────────────────
DISTILL_PROMPT = """You are extracting durable memories about Chris from a session transcript.

Rules:
- Output ONLY a JSON array, no prose, no markdown fences.
- Each entry: {{"content": "<one sentence>", "category": "preference|fact|decision|entity|other", "confidence": 0.0-1.0, "reason": "<why this is durable>", "context_tags": "<comma-separated contexts: coding,infra,personal,etc>", "override_conditions": "<when Chris would NOT follow this, or empty>"}}
- Maximum {max_n} entries. Fewer is better. Skip ephemeral chat.
- Only extract things that would still be true next month.
- Each content field must be self-contained — no pronouns referring to outside context.
- Skip anything you already know from Chris's profile (functional components, conventional commits, npm, Tailwind, shadcn, etc.) — only NEW signals.

Transcript:
\"\"\"
{transcript}
\"\"\"

Triggered passages (already scored as candidates):
{passages}

Output JSON array:"""


def distill_via_jenna(transcript: str, candidates: list[dict[str, Any]], max_n: int = MAX_PER_SESSION) -> list[dict[str, Any]]:
    """Dispatch to OpenClaw Jenna for structured memory extraction.

    Returns a list of memory dicts (may be empty). Failures return [] silently —
    the caller logs and proceeds without breaking the session.
    """
    if not candidates and len(transcript) < 200:
        return []

    passages_str = "\n".join(
        f"- [{c['triggers']}] {c['block'][:300]}" for c in candidates[:10]
    ) or "(none scored — extract from full transcript)"

    prompt = DISTILL_PROMPT.format(
        max_n=max_n,
        transcript=transcript[:4000],
        passages=passages_str,
    )

    result = _dispatch(
        agent="jenna",
        message=prompt,
        thinking="low",
        timeout=DISTILL_TIMEOUT_SEC,
    )
    if not result.ok:
        return []
    return _parse_loose_array(result.text)



def _parse_loose_array(text: str) -> list[dict[str, Any]]:
    """Best-effort JSON array extraction from LLM output."""
    if not text:
        return []
    text = text.strip()
    # Strip markdown fences if present
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    # Find the first [ ... ] block
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return []
    try:
        parsed = json.loads(match.group(0))
        if isinstance(parsed, list):
            return [m for m in parsed if isinstance(m, dict) and m.get("content")]
    except json.JSONDecodeError:
        pass
    return []


# ── Step 3: embed and store ─────────────────────────────────────────────
def embed_and_store(memories: list[dict[str, Any]], source: str, agent: str) -> list[dict[str, Any]]:
    """Embed via Ollama and upsert into semantic_memory. Returns the stored entries."""
    if not memories:
        return []

    ensure_collection(SEMANTIC_COLLECTION)
    col_id = _get_collection_id(SEMANTIC_COLLECTION)
    if not col_id:
        return []

    stored: list[dict[str, Any]] = []
    ids: list[str] = []
    embeddings: list[list[float]] = []
    documents: list[str] = []
    metadatas: list[dict[str, Any]] = []

    # Phase 1: Prepare embeddings and dedup outside the lock (HTTP calls to Ollama/ChromaDB)
    for mem in memories[:MAX_PER_SESSION]:
        content = (mem.get("content") or "").strip()
        if len(content) < 10:
            continue

        category = mem.get("category", "other")
        if category not in ("preference", "fact", "decision", "entity", "other"):
            category = "other"

        try:
            confidence = float(mem.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5

        mem_id = f"{SEMANTIC_COLLECTION}:{_digest(content)}"
        try:
            embedding = get_embedding(content[:EMBED_TRUNCATE], prefix="passage")
            # Use passage embedding for dedup — negligible accuracy difference for same-content similarity
        except Exception:
            continue
        if not embedding:
            continue

        now_iso = _now_iso()
        meta = {
            "agent": agent,
            "source": source,
            "category": category,
            "confidence": str(round(confidence, 3)),
            "reason": (mem.get("reason") or "")[:300],
            "context_tags": (mem.get("context_tags") or "")[:200],
            "override_conditions": (mem.get("override_conditions") or "")[:300],
            "created_at": now_iso,
            "type": "self_learning",
            "embed_model": EMBED_MODEL,
            "embed_model_version": EMBED_MODEL_VERSION,
            # Phase 1B: supersession chains — empty string = not superseded
            "supersedes": "",
            "superseded_by": "",
            # Phase 1C: temporal validity window
            "valid_from": now_iso,
            "valid_until": "",
            # Phase 1D: memory class tier (episodic → semantic → obsolete)
            "memory_class": "episodic",
            # Phase 1E: trust score for ranking (default 0.5)
            "trust_score": "0.5",
        }

        # Dedup layer 1: exact content hash match
        try:
            existing = chroma_api(
                "POST",
                f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/get",
                {"ids": [mem_id], "include": []},
            )
            if existing.get("ids") and mem_id in existing["ids"]:
                continue
        except Exception:
            pass

        # Phase 1A: Memory operations semantics (Mem0-inspired classification)
        operation = "ADD"
        supersede_target = None
        try:
            from memory_operations import classify_operation, should_delete_by_content
            # Always run classifier to find a target candidate
            op, target_id, _diag = classify_operation(
                content, embedding, confidence, col_id
            )
            supersede_target = target_id
            # DELETE takes precedence when explicit invalidation phrase present
            if should_delete_by_content(content):
                operation = "DELETE"
            else:
                operation = op
        except Exception as e:
            log.debug("classify_operation failed: %s — defaulting to ADD", e)

        if operation == "NOOP":
            continue

        if operation == "DELETE" and supersede_target:
            # Explicit invalidation phrase with a target — delete the target,
            # skip storing the invalidation statement as its own memory.
            try:
                chroma_api(
                    "POST",
                    f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/delete",
                    {"ids": [supersede_target]},
                )
                try:
                    from audit_log import log_event
                    log_event(
                        "delete",
                        entity_a=supersede_target,
                        entity_b="",
                        resolution="invalidation_phrase",
                        reason=f"DELETE classified from content: {content[:100]}",
                    )
                except Exception:
                    pass
            except Exception as e:
                log.warning("DELETE: failed to remove %s: %s", supersede_target, e)
            continue
        # DELETE without target → fall through to ADD (user said "forget X" but no match found)
        if operation == "DELETE":
            operation = "ADD"

        if operation == "UPDATE" and supersede_target:
            # Phase 1B: mark new memory as superseding the old one
            meta["supersedes"] = supersede_target
            # Mark old memory as superseded
            try:
                chroma_api(
                    "POST",
                    f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/update",
                    {
                        "ids": [supersede_target],
                        "metadatas": [{
                            "superseded_by": mem_id,
                            "valid_until": now_iso,
                        }],
                    },
                )
                try:
                    from audit_log import log_event
                    log_event(
                        "supersession",
                        entity_a=supersede_target,
                        entity_b=mem_id,
                        resolution="update_chain",
                        reason="Phase 1A classified as UPDATE — refinement of prior fact",
                    )
                except Exception:
                    pass
            except Exception as e:
                log.warning("failed to mark %s superseded: %s", supersede_target, e)

        ids.append(mem_id)
        embeddings.append(embedding)
        documents.append(content)
        metadatas.append(meta)
        stored.append({
            "id": mem_id,
            "content": content,
            "metadata": meta,
            "embedding": embedding,
            "operation": operation,
        })

    if not ids:
        return []

    # Phase 2: Upsert (ChromaDB handles its own concurrency)
    try:
        chroma_api(
            "POST",
            f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/upsert",
            {"ids": ids, "embeddings": embeddings, "documents": documents, "metadatas": metadatas},
        )
    except Exception as e:
        print(f"WARNING learn upsert failed: {e}")
        return []

    # Fire on_memory_stored hooks (one per stored memory)
    try:
        import hooks
        for entry in stored:
            hooks.fire(
                "on_memory_stored",
                mem_id=entry["id"],
                category=entry["metadata"].get("category", "other"),
                operation=entry.get("operation", "ADD"),
            )
    except Exception:
        pass

    # Phase 3: Extract entities into graph (fire-and-forget background thread)
    def _bg_extract():
        for entry in stored[:3]:
            try:
                from entity_graph import extract_and_store_entities
                extract_and_store_entities(entry["content"], entry["id"])
            except Exception:
                pass
    import threading
    threading.Thread(target=_bg_extract, daemon=True).start()

    return stored


# ── Step 4: contradiction detection ─────────────────────────────────────
def check_contradictions(stored: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """For each new memory, find existing entries that may contradict it.

    Heuristic: same category + high vector similarity (>0.85) + low token overlap (<0.5)
    means "we said something semantically related but lexically different" — likely
    a flip, edit, or correction. Flagged for human review via Brain UI.
    """
    if not stored:
        return []

    ensure_collection(CONTRADICTIONS_COLLECTION)
    contradictions: list[dict[str, Any]] = []

    sem_col_id = _get_collection_id(SEMANTIC_COLLECTION)
    if not sem_col_id:
        return []

    for mem in stored:
        try:
            res = chroma_api(
                "POST",
                f"/api/v2/tenants/default_tenant/databases/default_database/collections/{sem_col_id}/query",
                {
                    "query_embeddings": [mem["embedding"]],
                    "n_results": 8,
                    "include": ["documents", "metadatas", "distances"],
                },
            )
        except Exception:
            continue

        ids_lists = res.get("ids") or []
        if not ids_lists or not ids_lists[0]:
            continue
        ids = ids_lists[0]
        docs = (res.get("documents") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        metas_list = (res.get("metadatas") or [[]])[0]

        new_tokens = _tokenize(mem["content"])
        new_category = mem["metadata"]["category"]

        for other_id, other_doc, other_dist, other_meta in zip(ids, docs, dists, metas_list):
            if other_id == mem["id"]:
                continue
            # Skip cross-category comparisons — a preference can't contradict a fact
            other_category = (other_meta or {}).get("category", "")
            if other_category and other_category != new_category:
                continue
            # ChromaDB returns cosine distance in [0, 2] (0 = identical).
            # Use the declared SIMILARITY_THRESHOLD: distance > (1 - 0.85) = 0.15 → not similar enough.
            if other_dist > (1 - SIMILARITY_THRESHOLD):
                continue
            other_tokens = _tokenize(other_doc)
            overlap = _jaccard(new_tokens, other_tokens)
            if overlap >= TOKEN_DIVERGENCE_THRESHOLD:
                continue  # similar enough lexically — not a flip

            # Record the contradiction
            contradiction = {
                "id": f"contra:{uuid.uuid4().hex[:12]}",
                "new_id": mem["id"],
                "old_id": other_id,
                "new_content": mem["content"],
                "old_content": other_doc,
                "category": mem["metadata"]["category"],
                "distance": round(float(other_dist), 4),
                "token_overlap": round(overlap, 3),
                "created_at": _now_iso(),
                "review_state": "pending",
            }
            # Auto-resolve clear cases: newer + higher confidence wins
            new_conf = float(mem["metadata"].get("confidence", 0.5))
            old_conf = float((other_meta or {}).get("confidence", 0.5))
            new_time = mem["metadata"].get("created_at", "")
            old_time = (other_meta or {}).get("created_at", "")

            if new_conf - old_conf > 0.2 and new_time and old_time and new_time > old_time:
                # Auto-resolve: keep newer high-confidence entry. ALWAYS persist
                # the contradiction record first so the decision has an audit
                # trail — if the delete succeeds but no record exists, the
                # losing memory is gone with no recovery path.
                contradiction["review_state"] = "auto_resolved"
                contradiction["resolution"] = "keep_new"
                try:
                    _store_contradiction(contradiction)
                except Exception as e:
                    # Audit store failed — do NOT delete the losing memory.
                    # Downgrade the contradiction back to pending so the UI
                    # surfaces it for manual review instead of showing a
                    # falsely-resolved record with no backing audit row.
                    contradiction["review_state"] = "pending"
                    contradiction.pop("resolution", None)
                    contradictions.append(contradiction)
                    continue
                try:
                    chroma_api("POST",
                        f"/api/v2/tenants/default_tenant/databases/default_database/collections/{sem_col_id}/delete",
                        {"ids": [other_id]})
                    try:
                        from audit_log import log_event
                        log_event("resolve", entity_a=other_id, entity_b=mem["id"],
                                  match_score=round(float(other_dist), 3),
                                  conflict_type="contradiction", resolution="auto_keep_new",
                                  reason=f"Auto: newer ({new_time[:10]}) + higher conf ({new_conf:.2f} vs {old_conf:.2f})")
                    except Exception:
                        pass
                except Exception:
                    pass
            else:
                contradictions.append(contradiction)
                _store_contradiction(contradiction)

    return contradictions


def _store_contradiction(contradiction: dict[str, Any]) -> None:
    col_id = _get_collection_id(CONTRADICTIONS_COLLECTION)
    if not col_id:
        return
    summary = (
        f"NEW: {contradiction['new_content']}\n"
        f"OLD: {contradiction['old_content']}"
    )
    embedding = get_embedding(summary[:EMBED_TRUNCATE])
    if not embedding:
        return
    chroma_api(
        "POST",
        f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/upsert",
        {
            "ids": [contradiction["id"]],
            "embeddings": [embedding],
            "documents": [summary],
            "metadatas": [{
                "new_id": contradiction["new_id"],
                "old_id": contradiction["old_id"],
                "category": contradiction["category"],
                "distance": str(contradiction["distance"]),
                "token_overlap": str(contradiction["token_overlap"]),
                "created_at": contradiction["created_at"],
                "review_state": "pending",
            }],
        },
    )


# ── Public entry point ──────────────────────────────────────────────────
def process_session(transcript: str, source: str = "session", agent: str = "claude") -> dict[str, Any]:
    """Full pipeline: extract → distill → embed → contradict.

    Returns a summary dict the API can echo back. All errors are caught and
    surfaced in the result so a bad transcript never breaks the caller.
    """
    summary = {
        "candidates": 0,
        "distilled": 0,
        "stored": 0,
        "contradictions": 0,
        "errors": [],
    }

    try:
        candidates = extract_candidates(transcript)
        summary["candidates"] = len(candidates)
    except Exception as e:
        summary["errors"].append(f"extract: {e}")
        return summary

    try:
        memories = distill_via_jenna(transcript, candidates)
        summary["distilled"] = len(memories)
    except Exception as e:
        summary["errors"].append(f"distill: {e}")
        return summary

    try:
        stored = embed_and_store(memories, source=source, agent=agent)
        summary["stored"] = len(stored)
        summary["entries"] = [
            {"id": s["id"], "content": s["content"], "category": s["metadata"]["category"]}
            for s in stored
        ]
    except Exception as e:
        summary["errors"].append(f"store: {e}")
        return summary

    try:
        contradictions = check_contradictions(stored)
        summary["contradictions"] = len(contradictions)
    except Exception as e:
        summary["errors"].append(f"contradict: {e}")

    return summary


# ── CLI for manual testing ──────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Self-learning extraction pipeline")
    parser.add_argument("--transcript", help="Inline transcript text")
    parser.add_argument("--file", help="Read transcript from file")
    parser.add_argument("--source", default="cli")
    parser.add_argument("--agent", default="claude")
    args = parser.parse_args()

    text = args.transcript or (Path(args.file).read_text() if args.file else "")
    if not text:
        sys.stderr.write("Provide --transcript or --file\n")
        sys.exit(2)

    result = process_session(text, source=args.source, agent=args.agent)
    print(json.dumps(result, indent=2, ensure_ascii=False))
