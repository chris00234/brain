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
SIMILARITY_THRESHOLD = 0.90  # cosine similarity above this = potential contradiction
# Real contradictions share most of their wording but differ on a key term
# ("lives in Irvine" vs "lives in San Francisco"). Low-overlap pairs are
# almost always complementary, not contradictory.
MIN_CONTRADICTION_OVERLAP = 0.55
# Exclude "wants / would like / prefers / should" statements about broad topics
# from contradiction detection — these are additive preferences, not flips.
PREFERENCE_STOPWORDS = frozenset({
    "chris", "wants", "want", "prefers", "prefer", "likes", "like",
    "would", "should", "brain", "system", "the", "a", "to", "be",
})
DISTILL_TIMEOUT_SEC = 90
EMBED_TRUNCATE = 1000
SESSION_SUMMARY_MAX_LEN = 200

# ── Correction detection heuristics ───────────────────────────────────
CORRECTION_PATTERNS = [
    re.compile(r"(?:that'?s|that is) (?:wrong|incorrect|not right|not true)", re.IGNORECASE),
    re.compile(r"(?:no|nope),? (?:it'?s|it is|actually)", re.IGNORECASE),
    re.compile(r"the brain (?:said|thinks|returned) .+ but", re.IGNORECASE),
    re.compile(r"(?:wrong|incorrect|stale|outdated) (?:info|information|data|answer)", re.IGNORECASE),
]

# Round 9 B3: collections to scan for cross-source corroboration. A new fact
# mentioned in N of these gets a higher trust_score than a singleton.
CORROBORATION_COLLECTIONS = ("semantic_memory", "canonical", "experience", "knowledge")
TRUST_BASELINE = 0.4
TRUST_PER_SOURCE = 0.1
TRUST_MAX_SOURCES = 6


def _count_corroborating_trust(content: str) -> float:
    """Compute a trust score in [0.4, 1.0] based on how many distinct
    collections already contain a similar fact. Cheap embedding-search query
    against each collection; thresholds tuned conservatively to avoid
    false-positive corroboration on near-misses.
    """
    text = (content or "").strip()
    if len(text) < 20:
        return TRUST_BASELINE
    try:
        emb = get_embedding(text[:EMBED_TRUNCATE], prefix="query")
    except Exception:
        return TRUST_BASELINE
    if not emb:
        return TRUST_BASELINE

    matched = 0
    for col_name in CORROBORATION_COLLECTIONS:
        try:
            col_id = _get_collection_id(col_name)
            if not col_id:
                continue
            resp = chroma_api(
                "POST",
                f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/query",
                {"query_embeddings": [emb], "n_results": 1, "include": ["distances"]},
            )
            dists = (resp.get("distances") or [[]])[0] if isinstance(resp, dict) else []
            if dists and dists[0] is not None and float(dists[0]) <= 0.25:
                matched += 1
        except Exception:
            continue

    score = TRUST_BASELINE + TRUST_PER_SOURCE * min(matched, TRUST_MAX_SOURCES)
    return round(min(1.0, score), 3)

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


# ── Session summary extraction (heuristic, no LLM) ────────────────────
# Pattern: "Human:" or "User:" prefixed lines in the transcript
_USER_MSG_RE = re.compile(r"(?:^|\n)\s*(?:Human|User|Chris)\s*:\s*(.+)", re.IGNORECASE)


def _extract_session_summary(transcript: str) -> str | None:
    """Extract a 1-2 sentence session summary from the raw transcript.

    Strategy: find the last substantive user message (>20 chars, not a
    one-word reaction). Falls back to first 200 chars of transcript.
    No LLM call — pure regex/heuristic.
    """
    if not transcript or len(transcript) < 30:
        return None

    # Try to find user messages and pick the last substantive one
    matches = _USER_MSG_RE.findall(transcript)
    for msg in reversed(matches):
        msg = msg.strip()
        # Skip short reactions like "ok", "good", "thanks"
        if len(msg) > 20:
            return msg[:SESSION_SUMMARY_MAX_LEN].strip()

    # Fallback: first 200 chars of the transcript body
    clean = transcript.strip()[:SESSION_SUMMARY_MAX_LEN].strip()
    if len(clean) > 20:
        return clean
    return None


def _write_session_summary(transcript: str, source: str, agent: str) -> str | None:
    """Extract and persist a session summary to working memory. Returns the summary or None."""
    summary = _extract_session_summary(transcript)
    if not summary:
        return None
    try:
        from working_memory import add_session_summary
        add_session_summary(content=summary, agent=agent, source=source)
        log.info("session summary written: %.80s...", summary)
        return summary
    except Exception as e:
        log.warning("failed to write session summary: %s", e)
        return None


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
- Output ONLY valid JSON (no prose, no markdown fences) with this shape:
  {{"memories": [...], "corrections": [...]}}
- Each memory: {{"content": "<one sentence>", "category": "preference|fact|decision|entity|other", "confidence": 0.0-1.0, "reason": "<why this is durable>", "context_tags": "<comma-separated contexts: coding,infra,personal,etc>", "override_conditions": "<when Chris would NOT follow this, or empty>"}}
- Each correction: {{"wrong_claim": "<what the brain/agent said that was wrong>", "right_answer": "<what Chris said the correct answer is>", "domain": "<topic area: infra|coding|personal|general>"}}
- Maximum {max_n} memories. Fewer is better. Skip ephemeral chat.
- Only extract memories that would still be true next month.
- Each content field must be self-contained — no pronouns referring to outside context.
- Skip anything you already know from Chris's profile (functional components, conventional commits, npm, Tailwind, shadcn, etc.) — only NEW signals.
- For corrections: look for moments where Chris told the agent/brain it was wrong, gave a correction, or overrode a recommendation. Only include real factual corrections, not style preferences.
{correction_hint}
Transcript:
\"\"\"
{transcript}
\"\"\"

Triggered passages (already scored as candidates):
{passages}

Output JSON:"""


def _has_correction_signals(transcript: str) -> bool:
    """Check if transcript contains correction patterns worth highlighting."""
    return any(p.search(transcript) for p in CORRECTION_PATTERNS)


def distill_via_jenna(
    transcript: str, candidates: list[dict[str, Any]], max_n: int = MAX_PER_SESSION,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Dispatch to OpenClaw Jenna for structured memory + correction extraction.

    Returns (memories, corrections). Failures return ([], []) silently —
    the caller logs and proceeds without breaking the session.
    """
    if not candidates and len(transcript) < 200:
        return [], []

    passages_str = "\n".join(
        f"- [{c['triggers']}] {c['block'][:300]}" for c in candidates[:10]
    ) or "(none scored — extract from full transcript)"

    correction_hint = ""
    if _has_correction_signals(transcript):
        correction_hint = (
            "- NOTE: This session contains corrections — pay special attention "
            "to extracting what was wrong and what the right answer is.\n"
        )

    prompt = DISTILL_PROMPT.format(
        max_n=max_n,
        transcript=transcript[:4000],
        passages=passages_str,
        correction_hint=correction_hint,
    )

    result = _dispatch(
        agent="jenna",
        message=prompt,
        thinking="low",
        timeout=DISTILL_TIMEOUT_SEC,
    )
    if not result.ok:
        return [], []
    return _parse_distill_response(result.text)



def _parse_distill_response(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse Jenna's response into (memories, corrections).

    Handles both the new {memories, corrections} object format and the
    legacy bare-array format (backward compat).
    """
    if not text:
        return [], []
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    # Try object format first: {"memories": [...], "corrections": [...]}
    obj_match = re.search(r"\{.*\}", text, re.DOTALL)
    if obj_match:
        try:
            parsed = json.loads(obj_match.group(0))
            if isinstance(parsed, dict):
                memories = [
                    m for m in (parsed.get("memories") or [])
                    if isinstance(m, dict) and m.get("content")
                ]
                corrections = [
                    c for c in (parsed.get("corrections") or [])
                    if isinstance(c, dict) and c.get("wrong_claim")
                ]
                if memories or corrections:
                    return memories, corrections
        except json.JSONDecodeError:
            pass

    # Fallback: legacy bare array format
    arr_match = re.search(r"\[.*\]", text, re.DOTALL)
    if arr_match:
        try:
            parsed = json.loads(arr_match.group(0))
            if isinstance(parsed, list):
                memories = [m for m in parsed if isinstance(m, dict) and m.get("content")]
                return memories, []
        except json.JSONDecodeError:
            pass

    return [], []


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
        if category not in ("preference", "fact", "decision", "entity", "correction", "other"):
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
            # Phase 1E (Round 9 B3): trust_score derived from cross-source
            # corroboration count. 0.4 baseline + 0.1 per matching source.
            "trust_score": str(_count_corroborating_trust(mem.get("content", ""))),
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
                content, embedding, confidence, col_id,
                category=category,
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
                # Phase 3 atoms-truth-layer mirror: mark superseded + insert provenance edge
                try:
                    from atoms_store import mark_superseded
                    mark_superseded(supersede_target, mem_id)
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

    # Intra-batch dedup: if two memories in the same batch are about the same thing,
    # keep only the longer/more detailed one. This prevents chatty sessions from
    # storing 3-4 near-duplicate memories about the same topic.
    if len(ids) > 1:
        from math import sqrt

        def _cosine_sim(a: list[float], b: list[float]) -> float:
            dot = sum(x * y for x, y in zip(a, b))
            na = sqrt(sum(x * x for x in a))
            nb = sqrt(sum(y * y for y in b))
            return dot / (na * nb) if na and nb else 0.0

        keep = [True] * len(ids)
        for i in range(len(ids)):
            if not keep[i]:
                continue
            for j in range(i + 1, len(ids)):
                if not keep[j]:
                    continue
                sim = _cosine_sim(embeddings[i], embeddings[j])
                if sim > 0.90:  # near-duplicates within the batch
                    # Keep the longer (more detailed) one
                    if len(documents[i]) >= len(documents[j]):
                        keep[j] = False
                    else:
                        keep[i] = False
                        break

        if not all(keep):
            ids = [x for x, k in zip(ids, keep) if k]
            embeddings = [x for x, k in zip(embeddings, keep) if k]
            documents = [x for x, k in zip(documents, keep) if k]
            metadatas = [x for x, k in zip(metadatas, keep) if k]
            stored = [x for x, k in zip(stored, keep) if k]

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

    # Phase 3 atoms-truth-layer mirror + Phase 6 30-word discipline.
    # Best-effort, gated by BRAIN_ATOMS_ENABLED. Failures NEVER fail the
    # Chroma upsert. learn.py is async-friendly so we allow Jenna re-distill.
    try:
        from atoms_gate import enforce as _atoms_enforce
        from atoms_store import upsert_atom

        for entry in stored:
            meta = entry.get("metadata") or {}
            atom_text, atom_status, atom_quality = _atoms_enforce(
                entry["content"][:2000],
                allow_redistill=True,
            )
            upsert_atom(
                text=atom_text,
                chroma_id=entry["id"],
                kind=(meta.get("category") or "fact"),
                confidence=float(meta.get("confidence", 0.5) or 0.5),
                tier=(meta.get("memory_class") or "episodic"),
                distilled_by="jenna",
                collection_hint="semantic_memory",
                quality_score=atom_quality,
                provenance={
                    "session_source": source,
                    "agent": agent,
                    "atoms_gate_status": atom_status,
                },
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

    Heuristic (Round 11): real contradictions share most of their wording but
    differ on a key content word (e.g. "lives in Irvine" vs "lives in San
    Francisco"). We require:
      - same category, AND
      - cosine similarity >= SIMILARITY_THRESHOLD (near-duplicate embeddings), AND
      - token overlap >= MIN_CONTRADICTION_OVERLAP (lexically similar), AND
      - the symmetric-difference tokens are not all generic preference stopwords.

    Previous versions required LOW overlap, which was semantically backward —
    low overlap pairs are complementary/paraphrased, not contradictory, and
    that produced 31 false positives on one week of brain activity.
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
            # Require very high similarity (distance <= 1 - 0.90 = 0.10) — we
            # only want near-duplicates in embedding space.
            if other_dist > (1 - SIMILARITY_THRESHOLD):
                continue
            other_tokens = _tokenize(other_doc)
            overlap = _jaccard(new_tokens, other_tokens)
            # Real contradictions share most wording but differ on a key term.
            # Previous logic (flag when overlap < 0.5) was semantically backward
            # and produced mostly false positives on complementary preferences.
            if overlap < MIN_CONTRADICTION_OVERLAP:
                continue
            # Content-word diff check: ignore pairs whose only differences are
            # generic preference stopwords. A real contradiction must differ on
            # a content word that's not in the stopword set.
            sym_diff = (new_tokens ^ other_tokens) - PREFERENCE_STOPWORDS
            if not sym_diff:
                continue  # differences are all generic filler

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


# ── Correction recording ──────────────────────────────────────────────
def _record_corrections(
    corrections: list[dict[str, Any]], source: str, agent: str,
) -> int:
    """Record extracted corrections as negative outcomes + semantic memories.

    Each correction:
      1. Creates a negative outcome in accuracy_tracker (chris_override=True)
      2. Stores a correction memory in semantic_memory (category=correction, confidence=0.9)

    Returns count of successfully recorded corrections.
    """
    if not corrections:
        return 0

    recorded = 0
    for corr in corrections[:MAX_PER_SESSION]:
        wrong = (corr.get("wrong_claim") or "").strip()
        right = (corr.get("right_answer") or "").strip()
        domain = (corr.get("domain") or "general").strip()
        if not wrong or not right:
            continue

        # 1. Record negative outcome in accuracy tracker
        try:
            from task_queue import task_queue
            task_id = f"correction_{_digest(wrong + right)}"
            task_queue.record_outcome(
                task_id=task_id,
                domain=domain,
                brain_recommendation=wrong[:500],
                actual_action=right[:500],
                chris_override=True,
                override_reason=f"Session correction ({source}): brain said wrong thing",
            )
        except Exception as e:
            log.warning("failed to record correction outcome: %s", e)

        # 2. Store as semantic_memory so the brain remembers the mistake
        content = f"CORRECTION: Brain/agent said \"{wrong}\" but the correct answer is \"{right}\""
        mem = {
            "content": content,
            "category": "correction",
            "confidence": 0.9,
            "reason": "Extracted from session where Chris corrected the brain",
            "context_tags": domain,
            "override_conditions": "",
        }
        try:
            stored = embed_and_store([mem], source=source, agent=agent)
            if stored:
                recorded += 1
        except Exception as e:
            log.warning("failed to store correction memory: %s", e)

    if recorded:
        log.info("recorded %d corrections from session", recorded)
    return recorded


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
        "corrections": 0,
        "errors": [],
    }

    try:
        candidates = extract_candidates(transcript)
        summary["candidates"] = len(candidates)
    except Exception as e:
        summary["errors"].append(f"extract: {e}")
        return summary

    # Write session summary to working memory (heuristic, no LLM)
    try:
        session_text = _write_session_summary(transcript, source=source, agent=agent)
        if session_text:
            summary["session_summary"] = session_text
    except Exception as e:
        summary["errors"].append(f"session_summary: {e}")

    try:
        memories, corrections = distill_via_jenna(transcript, candidates)
        summary["distilled"] = len(memories)
    except Exception as e:
        summary["errors"].append(f"distill: {e}")
        return summary

    # Record corrections as negative outcomes + semantic memories
    try:
        n_corrections = _record_corrections(corrections, source=source, agent=agent)
        summary["corrections"] = n_corrections
    except Exception as e:
        summary["errors"].append(f"corrections: {e}")

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
