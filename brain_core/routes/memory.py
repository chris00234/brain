"""Memory CRUD + contradictions + /brain/timetravel.

Extracted from server.py. Routes use the shared VectorStore abstraction for
all memory reads/writes. Contradictions voting uses a separate sqlite table
co-located with session_context in autonomy.db.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager, suppress
from datetime import UTC
from typing import Annotated, Any, Literal

import learn
import search_unified
from api_deps import _safe_http_detail, log, verify_bearer
from conflict_resolver import recommend_resolution
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import Path as PathParam
from indexer import get_embedding as _get_embedding
from metrics_buffer import metrics_buffer as _metrics_buf
from pydantic import BaseModel, Field
from rate_limit import limiter
from scheduler import brain_scheduler
from vector_store import get_vector_store

from config import BRAIN_DIR

router = APIRouter(dependencies=[Depends(verify_bearer)])


@contextmanager
def _votes_conn():
    """SQLite connection for contradiction_votes table in autonomy.db."""
    db = BRAIN_DIR / "logs" / "autonomy.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS contradiction_votes (
                contradiction_id TEXT NOT NULL,
                voter_agent TEXT NOT NULL,
                vote TEXT NOT NULL,
                confidence REAL NOT NULL,
                reasoning TEXT,
                voted_at TEXT NOT NULL,
                PRIMARY KEY (contradiction_id, voter_agent)
            )
            """
        )
        yield conn
    finally:
        conn.close()


# ── Pydantic models ──────────────────────────────────
class MemoryEntry(BaseModel):
    id: str
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryListResponse(BaseModel):
    results: list[MemoryEntry]
    total: int
    limit: int
    offset: int


class MemoryCreateRequest(BaseModel):
    content: str = Field(..., min_length=5, max_length=2000)
    category: Literal["preference", "fact", "decision", "entity", "other"] = "other"
    agent: str = Field(default="claude", max_length=32)
    source: str = Field(default="manual", max_length=64)
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    reason: str = Field(default="", max_length=300)
    # M8.7: parent-child chunking. Optional parent atom id for callers that
    # want to store this memory as a child of a larger-context parent atom.
    # Retrieval can expand the child → parent when extra context is useful.
    parent_atom_id: str | None = Field(default=None, max_length=64)
    # 2026-04-26: explicit AI update intent. When the caller knows this new
    # atom REPLACES specific older atoms (e.g., user said "I work 8-6 now,
    # was 8-5"; or AI gave a wrong answer and user corrected), the caller
    # passes the older atom_ids here. The brain skips the cosine-similarity
    # supersession gate and directly marks each as superseded_by + sets
    # valid_until. Audit log records this as `explicit_update` so the
    # provenance is distinguishable from inferred supersession.
    replaces: list[str] | None = Field(default=None, description="atom_ids this new atom explicitly replaces")
    replaces_reason: str = Field(
        default="",
        max_length=300,
        description="why the caller knows these atoms are superseded (e.g., user-correction)",
    )


class MemoryPatchRequest(BaseModel):
    content: str | None = None
    category: Literal["preference", "fact", "decision", "entity", "other"] | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class ContradictionEntry(BaseModel):
    id: str
    new_content: str
    old_content: str
    category: str
    distance: float
    token_overlap: float
    review_state: str
    created_at: str
    metadata: dict = Field(default_factory=dict)
    recommendation: dict[str, Any] | None = None


class ContradictionListResponse(BaseModel):
    results: list[ContradictionEntry]
    total: int


class ContradictionResolveRequest(BaseModel):
    action: Literal["keep_new", "keep_old", "both_true", "merge", "dismiss"]


# ── Helpers ─────────────────────────────────────────────
def _memory_collection_id() -> str:
    get_vector_store().create_collection(learn.SEMANTIC_COLLECTION)
    return learn.SEMANTIC_COLLECTION


def _contradictions_collection_id() -> str:
    get_vector_store().create_collection(learn.CONTRADICTIONS_COLLECTION)
    return learn.CONTRADICTIONS_COLLECTION


# ── Routes: memory CRUD ─────────────────────────────────
# ── /memory GET response cache (30s TTL) ──
_memory_list_cache: dict[str, tuple[float, MemoryListResponse]] = {}
_memory_list_lock = threading.Lock()
# In-flight map: key → Event. Second caller with the same key waits for the
# first to finish and then re-reads the cache, instead of issuing a duplicate
# 300ms Chroma fetch. Prevents cache stampede on cold UI polls.
_memory_list_inflight: dict[str, threading.Event] = {}
_MEMORY_LIST_TTL = 30.0
_MEMORY_LIST_MAX = 100


def _memory_cache_key(limit: int, offset: int, category: str | None, agent: str | None) -> str:
    return f"{limit}:{offset}:{category or ''}:{agent or ''}"


@router.get("/memory", response_model=MemoryListResponse, tags=["memory"])
def list_memory(
    category: str | None = None,
    agent: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> MemoryListResponse:
    cache_key = _memory_cache_key(limit, offset, category, agent)
    now = time.time()
    with _memory_list_lock:
        entry = _memory_list_cache.get(cache_key)
        if entry and now - entry[0] < _MEMORY_LIST_TTL:
            return entry[1]
        inflight = _memory_list_inflight.get(cache_key)
        if inflight is None:
            # This caller is the primary — register the inflight marker.
            inflight = threading.Event()
            _memory_list_inflight[cache_key] = inflight
            is_primary = True
        else:
            is_primary = False

    if not is_primary:
        # Another caller is fetching — wait up to 5s then re-check cache.
        inflight.wait(timeout=5.0)
        with _memory_list_lock:
            entry = _memory_list_cache.get(cache_key)
            if entry and time.time() - entry[0] < _MEMORY_LIST_TTL:
                return entry[1]
        # Primary failed or timed out — fall through and do it ourselves.

    try:
        collection = _memory_collection_id()
        store = get_vector_store()

        where: dict[str, Any] = {}
        if category:
            where["category"] = category
        if agent:
            where["agent"] = agent
        chroma_where: dict[str, Any] | None = None
        if where:
            chroma_where = where if len(where) == 1 else {"$and": [{k: v} for k, v in where.items()]}

        # Vector store GET doesn't support ordering. Fetch up to 500 matching
        # entries, sort by created_at descending (newest first), then paginate
        # in-memory. 500-entry cap keeps response time under ~300ms.
        try:
            points = store.get(
                collection,
                filter=chroma_where,
                limit=min(limit * 3, 500),
                with_payload=True,
                with_documents=True,
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=_safe_http_detail("vector get", e))

        # Real total count (not just len of capped fetch).
        try:
            total = store.count(collection)
        except Exception:
            total = 0

        all_entries = [
            MemoryEntry(id=p.id, content=p.document or "", metadata=p.payload or {}) for p in points
        ]

        # Sort newest first by created_at
        all_entries.sort(
            key=lambda e: e.metadata.get("created_at") or e.metadata.get("updated_at") or "",
            reverse=True,
        )
        safe_limit = min(max(limit, 1), 200)
        safe_offset = max(offset, 0)
        page_entries = all_entries[safe_offset : safe_offset + safe_limit]

        response = MemoryListResponse(results=page_entries, total=total, limit=safe_limit, offset=safe_offset)

        with _memory_list_lock:
            _memory_list_cache[cache_key] = (time.time(), response)
            if len(_memory_list_cache) > _MEMORY_LIST_MAX:
                oldest = min(_memory_list_cache, key=lambda k: _memory_list_cache[k][0])
                del _memory_list_cache[oldest]

        return response
    finally:
        # Signal waiters and clear the inflight marker regardless of outcome.
        if is_primary:
            with _memory_list_lock:
                _memory_list_inflight.pop(cache_key, None)
            inflight.set()


@router.get(
    "/memory/contradictions",
    response_model=ContradictionListResponse,
    tags=["memory"],
)
def list_contradictions(limit: int = 50) -> ContradictionListResponse:
    collection = _contradictions_collection_id()
    store = get_vector_store()
    _where = {"review_state": "pending"}
    try:
        # Total count of pending contradictions (ids-only fetch).
        total_points = store.get(
            collection,
            filter=_where,
            limit=10000,
            with_payload=False,
            with_documents=False,
        )
        total = len(total_points)
        # Paginated fetch with content
        points = store.get(
            collection,
            filter=_where,
            limit=min(max(limit, 1), 200),
            with_payload=True,
            with_documents=True,
        )
    except Exception:
        return ContradictionListResponse(results=[], total=0)

    semantic_ids: set[str] = set()
    for p in points:
        meta = p.payload or {}
        if meta.get("old_id"):
            semantic_ids.add(str(meta["old_id"]))
        if meta.get("new_id"):
            semantic_ids.add(str(meta["new_id"]))
    semantic_meta: dict[str, dict[str, Any]] = {}
    if semantic_ids:
        try:
            semantic_points = store.get(
                _memory_collection_id(),
                ids=list(semantic_ids),
                with_payload=True,
                with_documents=False,
            )
            semantic_meta = {sp.id: dict(sp.payload or {}) for sp in semantic_points}
        except Exception:
            semantic_meta = {}

    entries: list[ContradictionEntry] = []
    for p in points:
        i = p.id
        doc = p.document or ""
        meta = p.payload or {}
        old_id = str(meta.get("old_id") or "")
        new_id = str(meta.get("new_id") or "")
        new_content = ""
        old_content = ""
        if doc:
            current_section = None
            for line in doc.split("\n"):
                if line.startswith("NEW: "):
                    current_section = "new"
                    new_content = line[5:]
                elif line.startswith("OLD: "):
                    current_section = "old"
                    old_content = line[5:]
                elif current_section == "new":
                    new_content += "\n" + line
                elif current_section == "old":
                    old_content += "\n" + line
        recommendation = None
        if old_id or new_id:
            recommendation = recommend_resolution(
                meta,
                semantic_meta.get(old_id, {}),
                semantic_meta.get(new_id, {}),
                old_exists=old_id in semantic_meta,
                new_exists=new_id in semantic_meta,
            ).to_dict()
        entries.append(
            ContradictionEntry(
                id=i,
                new_content=new_content,
                old_content=old_content,
                category=meta.get("category", ""),
                distance=float(meta.get("distance", 0)),
                token_overlap=float(meta.get("token_overlap", 0)),
                review_state=meta.get("review_state", "pending"),
                created_at=meta.get("created_at", ""),
                metadata=meta,
                recommendation=recommendation,
            )
        )
    return ContradictionListResponse(results=entries, total=total)


@router.get("/memory/export", tags=["memory"])
def export_memory() -> list[dict]:
    """Export all semantic_memory entries as a JSON array for backup/migration."""
    collection = _memory_collection_id()
    store = get_vector_store()
    # Single call — QdrantStore.get walks Qdrant's native cursor internally
    # to honor the requested limit. No need for offset-based pagination
    # here (that path used to loop infinitely before the cursor-based fix).
    try:
        points = store.get(
            collection,
            limit=1_000_000,
            with_payload=True,
            with_documents=True,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=_safe_http_detail("vector get", e))
    return [{"id": p.id, "content": p.document or "", "metadata": p.payload or {}} for p in points]


@router.get("/memory/{mem_id}", response_model=MemoryEntry, tags=["memory"])
def get_memory(mem_id: Annotated[str, PathParam()]) -> MemoryEntry:
    collection = _memory_collection_id()
    try:
        points = get_vector_store().get(
            collection,
            ids=[mem_id],
            with_payload=True,
            with_documents=True,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=_safe_http_detail("vector get", e))
    if not points:
        raise HTTPException(status_code=404, detail=f"memory '{mem_id}' not found")
    p = points[0]
    return MemoryEntry(id=p.id, content=p.document or "", metadata=p.payload or {})


@router.post("/memory", response_model=MemoryEntry, tags=["memory"])
@limiter.limit("30/minute")
def create_memory(request: Request, req: MemoryCreateRequest) -> MemoryEntry:
    """Direct memory insert with Phase 1 lifecycle (operations, supersession, temporal, tiers)."""
    # M7-WS8: infer actor from header/query-param when caller left the default.
    # Goal: kill the 518/534 atoms with provenance.agent="?" problem.
    if not req.agent or req.agent in {"mcp", "unknown", "claude", "?"}:
        header_actor = request.headers.get("x-agent")
        query_actor = request.query_params.get("actor")
        inferred = header_actor or query_actor
        if inferred:
            req.agent = inferred

    # Layer A — test data gate. Reject test harness writes so brain's truth
    # layer never gets polluted by verification runs. Deterministic regex.
    from brain_core import test_gate

    is_test, reason = test_gate.is_test_context(
        source=req.source,
        content=req.content,
        agent=req.agent,
    )
    if is_test:
        raise HTTPException(
            status_code=400,
            detail=f"test_data_blocked: {reason}. Brain refuses to ingest test "
            f"fixtures into semantic_memory. Use a scratch collection or "
            f"session_context if you need test persistence.",
        )

    collection = _memory_collection_id()
    store = get_vector_store()

    mem_id = f"{learn.SEMANTIC_COLLECTION}:{learn._digest(req.content)}"
    embedding = _get_embedding(req.content[: learn.EMBED_TRUNCATE])
    if not embedding:
        raise HTTPException(status_code=502, detail="embedding failed")

    now_iso = learn._now_iso()

    # Phase 1A: Memory operations classification (Mem0-inspired)
    operation = "ADD"
    supersede_target = None
    try:
        from memory_operations import classify_operation, should_delete_by_content

        # Always run classify_operation to find a target (for DELETE/UPDATE/NOOP)
        op, target_id, _diag = classify_operation(
            req.content,
            embedding,
            req.confidence,
            collection,
            category=req.category,
        )
        supersede_target = target_id
        # DELETE takes precedence over UPDATE when explicit invalidation phrase present
        operation = "DELETE" if should_delete_by_content(req.content) else op
    except Exception:
        pass

    # NOOP: don't store, return existing memory ID
    if operation == "NOOP":
        return MemoryEntry(
            id=mem_id,
            content=req.content,
            metadata={"operation": "NOOP", "reason": "duplicate of existing memory"},
        )

    # DELETE: invalidation phrase — remove target if found, don't store the phrase.
    # If no target found, fall through to ADD (user said "forget X" but brain had no X).
    if operation == "DELETE" and supersede_target:
        with suppress(Exception):
            store.delete(collection, ids=[supersede_target])
        return MemoryEntry(
            id=supersede_target,
            content=req.content,
            metadata={
                "operation": "DELETE",
                "deleted_target": supersede_target,
                "reason": "invalidation phrase",
            },
        )
    # DELETE without target → fall through to ADD (not a real invalidation)
    if operation == "DELETE":
        operation = "ADD"

    metadata = {
        "agent": req.agent,
        "source": req.source,
        "category": req.category,
        # Phase A4: typed float so Qdrant payload range filters work.
        "confidence": round(float(req.confidence), 3),
        "reason": req.reason,
        "created_at": now_iso,
        "type": "manual",
        # Phase 2A: embedding version tracking
        "embed_model_version": learn.EMBED_MODEL_VERSION,
        # Phase 1B: supersession chains
        "supersedes": supersede_target or "",
        "superseded_by": "",
        # Phase 1C: temporal validity window
        "valid_from": now_iso,
        "valid_until": "",
        # Phase 1D: memory class tier (new memories start episodic)
        "memory_class": "episodic",
        # Phase 1E: trust score (typed float per Phase A4)
        "trust_score": 0.5,
    }

    # Phase 1B: on UPDATE, mark old memory as superseded
    if operation == "UPDATE" and supersede_target:
        with suppress(Exception):
            store.update_payload(
                collection,
                ids=[supersede_target],
                patch={"superseded_by": mem_id, "valid_until": now_iso},
            )

    try:
        store.upsert(
            collection,
            ids=[mem_id],
            vectors=[embedding],
            documents=[req.content],
            payloads=[metadata],
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=_safe_http_detail("vector upsert", e))

    _metrics_buf.record_memory_write()
    # Fire hook (Phase 6A; extended 2026-04-27 with content/confidence/agent
    # so hooks don't need a brain.db lookup — the atom mirror happens AFTER
    # this fire, so brain.db lookups would race the mirror write).
    try:
        import hooks

        hooks.fire(
            "on_memory_stored",
            mem_id=mem_id,
            category=req.category,
            operation=operation,
            content=req.content,
            confidence=float(req.confidence),
            agent=req.agent,
        )
    except Exception:
        pass

    # CR7 fix (2026-04-14): atoms mirror + v3 Brain Hygiene pipeline is now
    # a shared helper (ingest_mirror.mirror_memory) so /memory/batch, /learn,
    # and wm_consolidate can reuse the exact same block. Previously only
    # POST /memory went through the hygiene pipeline — batch was an
    # implicit bypass. HR4 fix: log errors instead of bare-except swallow.
    try:
        from atoms_store import apply_explicit_replaces, mark_superseded
        from ingest_mirror import mirror_memory

        _mr = mirror_memory(
            content=req.content,
            chroma_id=mem_id,
            category=req.category or "fact",
            agent=req.agent,
            source=req.source,
            operation=operation,
            confidence=req.confidence,
            parent_atom_id=req.parent_atom_id,
            now_iso=now_iso,
            allow_redistill=False,  # POST /memory is sync — don't block on Jenna
        )
        if _mr.error:
            log.warning(
                "atoms_mirror_failed mem_id=%s error=%s warnings=%s",
                mem_id,
                _mr.error,
                _mr.warnings,
            )
        elif _mr.warnings:
            log.info("atoms_mirror_warnings mem_id=%s warnings=%s", mem_id, _mr.warnings)

        if operation == "UPDATE" and supersede_target:
            mark_superseded(supersede_target, mem_id)

        # Explicit AI/user update intent — bypass the cosine gate and directly
        # supersede the named atoms. See atoms_store.apply_explicit_replaces.
        if req.replaces:
            _explicit = apply_explicit_replaces(
                mem_id,
                req.replaces,
                reason=req.replaces_reason or req.reason,
                agent=req.agent,
            )
            if _explicit.get("error"):
                log.warning(
                    "explicit_replaces_failed mem_id=%s error=%s",
                    mem_id,
                    _explicit["error"],
                )
            else:
                log.info(
                    "explicit_replaces_applied mem_id=%s applied=%s skipped=%s",
                    mem_id,
                    _explicit["applied"],
                    _explicit["skipped"],
                )
        # Attribute the producing prompt — manual /memory POST calls don't
        # use a distill prompt, so they get a synthetic "manual_v1" id.
        # Lets prompt_attribution.survival_report distinguish manual writes
        # (typically high-survival) from distilled-from-transcript atoms.
        try:
            from brain_core.prompt_attribution import record as _attr_record

            _attr_record(mem_id, "manual", "manual_v1")
        except Exception:
            pass
    except Exception as _e:
        log.warning("atoms_mirror_outer_exception mem_id=%s error=%s", mem_id, str(_e)[:200])

    response_meta = dict(metadata)
    response_meta["operation"] = operation

    # Phase N1: hot-path contradiction detection. Same heuristic as /learn,
    # runs inline so manual writes don't silently pollute retrieval. Killable
    # via BRAIN_CONTRADICT_ON_WRITE=0 without touching code paths.
    contradictions: list[dict] = []
    if os.environ.get("BRAIN_CONTRADICT_ON_WRITE", "1") != "0":
        try:
            contradictions = learn.check_contradictions_for_memory(
                mem_id=mem_id,
                content=req.content,
                embedding=embedding,
                category=req.category,
                confidence=req.confidence,
                created_at=now_iso,
                sem_col_id=collection,
            )
            if contradictions:
                response_meta["contradictions"] = [
                    {
                        "id": c["id"],
                        "old_id": c["old_id"],
                        "review_state": c["review_state"],
                        "distance": c["distance"],
                    }
                    for c in contradictions
                ]
                # Phase G2: pending (unresolved) contradiction → mark new atom
                # provisional so search_unified hides it from retrieval until
                # /memory/contradictions/{id}/resolve runs. Auto-resolved cases
                # (keep_new / keep_old / dismiss / merge inside check_contradictions)
                # never carry review_state="pending", so they bypass this gate.
                if any(c.get("review_state") == "pending" for c in contradictions):
                    try:
                        from brain_core.atoms_store import update_provisional_flag

                        if update_provisional_flag(mem_id, True):
                            response_meta["provisional"] = True
                    except Exception:
                        pass
        except Exception:
            pass

    # Phase N2: corroboration probe — if the new memory is a near-duplicate of
    # siblings that the contradiction check did NOT flag (i.e. they share
    # intent, not conflict), bump their confidence up via the evidence ledger.
    # Bounded to at most 3 sibling bumps per write so the O(n) probe stays
    # cheap and POST /memory p95 doesn't regress. Gated by
    # BRAIN_CORROBORATE_ON_WRITE (default on). Any exception is swallowed —
    # N2 is best-effort while brain_db migrates to @7.
    if os.environ.get("BRAIN_CORROBORATE_ON_WRITE", "1") != "0":
        try:
            contradict_old_ids = {c["old_id"] for c in (contradictions or [])}
            hits = get_vector_store().query(
                collection,
                vector=embedding,
                k=5,
                with_payload=True,
            )
            sibling_ids = [h.id for h in hits]
            # Preserve the distance-based variables downstream code expects.
            # ChromaStore returns similarity; re-derive cosine distance here.
            sibling_dists = [max(0.0, 1.0 - h.score) for h in hits]
            sibling_metas = [h.payload or {} for h in hits]
            from brain_core.atoms_store import (
                cluster_size_for as _cluster_size,
            )
            from brain_core.atoms_store import (
                derive_atom_id as _derive_atom_id,
            )
            from brain_core.atoms_store import (
                update_atom_confidence as _uac,
            )

            bumped = 0
            for sib_id, sib_dist, sib_meta in zip(sibling_ids, sibling_dists, sibling_metas, strict=False):
                if bumped >= 3:
                    break
                if sib_id == mem_id or sib_id in contradict_old_ids:
                    continue
                if sib_dist > 0.20:
                    continue
                if (sib_meta or {}).get("category") != req.category:
                    continue
                cluster = _cluster_size(sib_id, embedding)
                _uac(
                    atom_id=_derive_atom_id(sib_id),
                    event_type="corroborate",
                    weight=0.5,
                    evidence_ref=_derive_atom_id(mem_id),
                    cluster_size=cluster,
                )
                bumped += 1
        except Exception:
            pass

    # M7-WS8: action_audit insert for brain_store adoption tracking.
    try:
        from brain_core.atoms_store import insert_action_audit as _iaa

        _iaa(
            route="/memory",
            tool="brain_store",
            actor=req.agent or "unknown",
            query_text=req.content[:500],
            retrieved_chroma_ids=[mem_id],
        )
    except Exception:
        pass

    # 2026-04-17 (E wiring): auto-attribute valence when the caller tagged the
    # store with a positive/negative source per CLAUDE.md self-learning protocol.
    # Keeps the amygdala-style affective layer populated automatically as Chris
    # interacts, no manual tagging required. Fails open — valence is a nice-to-
    # have, not a write-path dependency.
    try:
        from brain_core import valence as _val

        src_lc = (req.source or "").lower()
        cat_lc = (req.category or "").lower()
        delta = 0.0
        if "positive_trigger" in src_lc or "praise" in src_lc:
            delta = 0.6
        elif "negative_trigger" in src_lc or "correction" in src_lc or cat_lc == "correction":
            delta = -0.6
        elif cat_lc == "preference" and "chris" in (req.content or "").lower():
            delta = 0.2  # mild positive — explicit preferences lean affirmative
        if delta != 0.0:
            _val.record_valence(
                atom_id=mem_id,
                delta=delta,
                reason=(req.reason or req.source or "")[:200],
                source=f"auto:{req.source or 'memory_post'}",
            )
    except Exception:
        pass

    return MemoryEntry(id=mem_id, content=req.content, metadata=response_meta)


class MemoryBatchRequest(BaseModel):
    memories: list[MemoryCreateRequest] = Field(..., min_length=1, max_length=50)


@router.post("/memory/batch", tags=["memory"])
@limiter.limit("10/minute")  # Phase M5: bulk write — same envelope as /learn
def create_memory_batch(request: Request, req: MemoryBatchRequest) -> dict:
    """Batch insert memories — 10x faster than single /memory calls.

    Each memory still gets individual classification (ADD/UPDATE/NOOP/DELETE)
    but the final ChromaDB upsert is a single batched call.
    """
    col_id = _memory_collection_id()  # collection name under VectorStore
    from memory_operations import classify_operation, should_delete_by_content

    ids_to_upsert = []
    embeddings_to_upsert = []
    docs_to_upsert = []
    metas_to_upsert = []
    operations = []
    supersede_updates: list[tuple[str, str, str]] = []  # (old_id, new_id, now_iso)
    deletes_to_apply: list[str] = []
    results = []

    for mem_req in req.memories:
        mem_id = f"{learn.SEMANTIC_COLLECTION}:{learn._digest(mem_req.content)}"
        embedding = _get_embedding(mem_req.content[: learn.EMBED_TRUNCATE])
        if not embedding:
            results.append({"id": mem_id, "operation": "SKIP", "reason": "embedding failed"})
            continue

        now_iso = learn._now_iso()
        operation = "ADD"
        supersede_target = None
        try:
            op, target_id, _diag = classify_operation(
                mem_req.content, embedding, mem_req.confidence, col_id, category=mem_req.category
            )
            supersede_target = target_id
            operation = "DELETE" if should_delete_by_content(mem_req.content) else op
        except Exception:
            pass

        if operation == "NOOP":
            results.append({"id": mem_id, "operation": "NOOP"})
            continue

        if operation == "DELETE" and supersede_target:
            deletes_to_apply.append(supersede_target)
            results.append({"id": supersede_target, "operation": "DELETE"})
            continue
        if operation == "DELETE":
            operation = "ADD"

        metadata = {
            "agent": mem_req.agent,
            "source": mem_req.source,
            "category": mem_req.category,
            # Phase A4: typed floats so Qdrant payload range filters work.
            "confidence": round(float(mem_req.confidence), 3),
            "reason": mem_req.reason,
            "created_at": now_iso,
            "type": "manual",
            "embed_model_version": learn.EMBED_MODEL_VERSION,
            "supersedes": supersede_target or "",
            "superseded_by": "",
            "valid_from": now_iso,
            "valid_until": "",
            "memory_class": "episodic",
            "trust_score": 0.5,
        }

        if operation == "UPDATE" and supersede_target:
            supersede_updates.append((supersede_target, mem_id, now_iso))

        ids_to_upsert.append(mem_id)
        embeddings_to_upsert.append(embedding)
        docs_to_upsert.append(mem_req.content)
        metas_to_upsert.append(metadata)
        operations.append(operation)
        results.append({"id": mem_id, "operation": operation})

    store = get_vector_store()

    # Apply supersede updates (batched).
    # Each row patches only the two supersede fields, per-id — update_payload
    # takes a single patch dict so we issue one call per id. The total batch
    # is usually small (<5), and read-merge-write inside ChromaStore preserves
    # the rest of the old row's metadata.
    if supersede_updates:
        try:
            for old_id, new_id, ts in supersede_updates:
                store.update_payload(
                    col_id,
                    ids=[old_id],
                    patch={"superseded_by": new_id, "valid_until": ts},
                )
        except Exception:
            pass

    # Apply deletes (batched)
    if deletes_to_apply:
        with suppress(Exception):
            store.delete(col_id, ids=deletes_to_apply)

    # Apply upserts (batched)
    if ids_to_upsert:
        try:
            store.upsert(
                col_id,
                ids=ids_to_upsert,
                vectors=embeddings_to_upsert,
                documents=docs_to_upsert,
                payloads=metas_to_upsert,
            )
            for _ in ids_to_upsert:
                _metrics_buf.record_memory_write()
        except Exception as e:
            raise HTTPException(status_code=502, detail=_safe_http_detail("batch upsert", e))

    # CR7 fix (2026-04-14): run the atoms-mirror + hygiene pipeline for
    # every batched write. Previously batch bypassed atoms_store entirely,
    # so batched memories had no hygiene fields, no topic supersession,
    # and no llm_backlog catch-up — an implicit Layer A bypass.
    try:
        from ingest_mirror import mirror_memory

        for mem_id_w, mem_req_w, op_w, meta_w in zip(
            ids_to_upsert, req.memories, operations, metas_to_upsert, strict=False
        ):
            _mr = mirror_memory(
                content=mem_req_w.content,
                chroma_id=mem_id_w,
                category=mem_req_w.category or "fact",
                agent=mem_req_w.agent,
                source=mem_req_w.source,
                operation=op_w,
                confidence=mem_req_w.confidence,
                parent_atom_id=None,
                now_iso=meta_w.get("created_at", ""),
                allow_redistill=False,
            )
            if _mr.error:
                log.warning(
                    "atoms_mirror_batch_failed mem_id=%s error=%s",
                    mem_id_w,
                    _mr.error,
                )
            try:
                from brain_core.prompt_attribution import record as _attr_record

                _attr_record(mem_id_w, "manual", "manual_v1")
            except Exception:
                pass
    except Exception as _e:
        log.warning("atoms_mirror_batch_outer error=%s", str(_e)[:200])

    # Fire hooks for stored memories
    try:
        import hooks

        for mem_id, op in zip(ids_to_upsert, operations, strict=False):
            hooks.fire("on_memory_stored", mem_id=mem_id, category="batch", operation=op)
    except Exception:
        pass

    # Phase N1: hot-path contradiction detection for the batch. Post-upsert
    # so the nearest-neighbor query sees the newly-written siblings. One
    # call per just-written memory (already in-process, no LLM roundtrip).
    # Killable via BRAIN_CONTRADICT_ON_WRITE=0.
    batch_contradictions: dict[str, list[dict]] = {}
    if ids_to_upsert and os.environ.get("BRAIN_CONTRADICT_ON_WRITE", "1") != "0":
        for mem_id_w, emb_w, doc_w, meta_w in zip(
            ids_to_upsert, embeddings_to_upsert, docs_to_upsert, metas_to_upsert, strict=False
        ):
            try:
                found = learn.check_contradictions_for_memory(
                    mem_id=mem_id_w,
                    content=doc_w,
                    embedding=emb_w,
                    category=meta_w.get("category", ""),
                    confidence=float(meta_w.get("confidence", 0.5) or 0.5),
                    created_at=meta_w.get("created_at", ""),
                    sem_col_id=col_id,
                )
                if found:
                    batch_contradictions[mem_id_w] = [
                        {
                            "id": c["id"],
                            "old_id": c["old_id"],
                            "review_state": c["review_state"],
                            "distance": c["distance"],
                        }
                        for c in found
                    ]
            except Exception:
                continue

    if batch_contradictions:
        # Phase G2: same gate as the single /memory path — pending contradictions
        # mark the new atom provisional so it stays out of retrieval until a
        # resolve action runs. Auto-resolved (non-pending) cases are untouched.
        try:
            from brain_core.atoms_store import update_provisional_flag as _upf
        except Exception:
            _upf = None
        for r in results:
            rid = r.get("id")
            if rid in batch_contradictions:
                r["contradictions"] = batch_contradictions[rid]
                if _upf is not None and any(
                    c.get("review_state") == "pending" for c in batch_contradictions[rid]
                ):
                    try:
                        if _upf(rid, True):
                            r["provisional"] = True
                    except Exception:
                        pass

    return {
        "stored": len(ids_to_upsert),
        "superseded": len(supersede_updates),
        "deleted": len(deletes_to_apply),
        "total_requested": len(req.memories),
        "contradictions_found": sum(len(v) for v in batch_contradictions.values()),
        "results": results,
    }


@router.patch("/memory/{mem_id}", response_model=MemoryEntry, tags=["memory"])
def patch_memory(mem_id: Annotated[str, PathParam()], req: MemoryPatchRequest) -> MemoryEntry:
    collection = _memory_collection_id()
    store = get_vector_store()

    # Read existing
    existing = get_memory(mem_id)
    new_content = req.content if req.content is not None else existing.content
    new_meta = dict(existing.metadata)
    patch: dict[str, Any] = {"updated_at": learn._now_iso()}
    if req.category is not None:
        new_meta["category"] = req.category
        patch["category"] = req.category
    if req.confidence is not None:
        # Phase A4: typed float, not stringified.
        new_meta["confidence"] = round(float(req.confidence), 3)
        patch["confidence"] = new_meta["confidence"]
    new_meta["updated_at"] = patch["updated_at"]

    try:
        if req.content is not None:
            # Content changed → re-embed and overwrite the whole point.
            embedding = _get_embedding(new_content[: learn.EMBED_TRUNCATE])
            if not embedding:
                raise HTTPException(status_code=502, detail="embedding failed")
            store.upsert(
                collection,
                ids=[mem_id],
                vectors=[embedding],
                documents=[new_content],
                payloads=[new_meta],
            )
        else:
            # Metadata-only patch — keep the existing vector untouched.
            store.update_payload(collection, ids=[mem_id], patch=patch)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=_safe_http_detail("vector upsert", e))
    return MemoryEntry(id=mem_id, content=new_content, metadata=new_meta)


@router.get("/brain/doubt", tags=["autonomy"])
def brain_doubt(limit: int = Query(default=20, ge=1, le=100)) -> dict:
    """2026-04-16 Tier 3 #8: metacognitive doubt surface.

    Returns things the brain is currently uncertain about, for the caller
    (Chris or an agent) to review/resolve. Superhuman brains must know
    what they don't know — surfacing uncertainty is more valuable than
    pretending confidence.

    Response shape:
      {
        "low_confidence_atoms": [...]  # atoms.confidence < 0.4, active tier
        "pending_contradictions": [...]  # unresolved semantic_contradictions
        "stale_canonical": [...]  # canonical notes >180d without review
        "open_loops": {...}  # unresolved commitments / waiting-on items
      }
    """
    import sqlite3 as _sql

    out: dict = {
        "low_confidence_atoms": [],
        "pending_contradictions": [],
        "stale_canonical": [],
        "open_loops": {"items": [], "total": 0, "stale_count": 0},
    }

    # Low-confidence atoms
    try:
        from atoms_store import _conn as _ac

        with _ac() as _c:
            rows = _c.execute(
                "SELECT id, text, confidence, trust_score, kind, tier, updated_at "
                "FROM atoms "
                "WHERE tier != 'obsolete' AND confidence < 0.4 "
                "ORDER BY confidence ASC LIMIT ?",
                (limit,),
            ).fetchall()
        out["low_confidence_atoms"] = [
            {
                "id": r["id"],
                "text": (r["text"] or "")[:240],
                "confidence": round(float(r["confidence"] or 0), 3),
                "trust_score": round(float(r["trust_score"] or 0), 3),
                "kind": r["kind"],
                "tier": r["tier"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]
    except (ImportError, _sql.Error):
        pass

    # Pending contradictions
    try:
        points = get_vector_store().get(
            "semantic_contradictions",
            limit=limit,
            with_payload=True,
            with_documents=True,
        )
        for p in points:
            m = p.payload or {}
            if m.get("resolved"):
                continue
            out["pending_contradictions"].append(
                {
                    "id": p.id,
                    "preview": (p.document or "")[:200],
                    "memory_id_a": m.get("memory_id_a"),
                    "memory_id_b": m.get("memory_id_b"),
                    "created_at": m.get("created_at"),
                }
            )
    except Exception:
        pass

    # Open-loop / commitment doubt surface. Best-effort and read-only: a
    # failed scan must not break existing low-confidence/contradiction output.
    try:
        from brain_core.open_loops import open_loop_snapshot

        out["open_loops"] = open_loop_snapshot(
            brain_db_path=BRAIN_DIR / "logs" / "brain.db",
            autonomy_db_path=BRAIN_DIR / "logs" / "autonomy.db",
            limit=limit,
        )
    except Exception:
        pass

    return out


@router.get("/brain/open-loops", tags=["autonomy"])
def brain_open_loops(
    limit: int = Query(default=20, ge=1, le=100),
    stale_days: int = Query(default=14, ge=1, le=365),
) -> dict:
    """Unresolved commitments, follow-ups, waiting-on items, and stale tasks."""
    try:
        from brain_core.open_loops import open_loop_snapshot

        return open_loop_snapshot(
            brain_db_path=BRAIN_DIR / "logs" / "brain.db",
            autonomy_db_path=BRAIN_DIR / "logs" / "autonomy.db",
            limit=limit,
            stale_days=stale_days,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("open_loops", e)) from e


@router.get("/brain/replacement-readiness", tags=["autonomy"])
def brain_replacement_readiness() -> dict:
    """Capability readiness gate for Brain as Chris-memory substitute."""
    try:
        from brain_core.brain_replacement_readiness import readiness_snapshot

        return readiness_snapshot()
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("replacement_readiness", e)) from e


@router.post("/brain/consolidate", tags=["autonomy"])
def brain_consolidate_trigger() -> dict:
    """2026-04-16 Tier 3 #8: on-demand sleep consolidation trigger.

    Superhuman brains should be able to consolidate on explicit demand
    (e.g. after a burst of learning), not only on the nightly schedule.
    Wraps the existing sleep_consolidate job dispatch.
    """
    try:
        pid = brain_scheduler.trigger_now("sleep_consolidate")
        return {"status": "dispatched", "job": "sleep_consolidate", "pid": pid}
    except Exception as e:
        raise HTTPException(status_code=502, detail=_safe_http_detail("consolidate dispatch", e))


@router.delete("/memory/{mem_id}", tags=["memory"])
def delete_memory(mem_id: Annotated[str, PathParam()]) -> dict:
    collection = _memory_collection_id()
    try:
        get_vector_store().delete(collection, ids=[mem_id])
    except Exception as e:
        raise HTTPException(status_code=502, detail=_safe_http_detail("vector delete", e))
    return {"status": "deleted", "id": mem_id}


@router.post("/memory/contradictions/{contra_id}/resolve", tags=["memory"])
def resolve_contradiction(
    contra_id: Annotated[str, PathParam()],
    req: ContradictionResolveRequest,
) -> dict:
    contra_col = _contradictions_collection_id()
    sem_col = _memory_collection_id()
    store = get_vector_store()

    # Read the contradiction record
    try:
        points = store.get(contra_col, ids=[contra_id], with_payload=True, with_documents=False)
    except Exception as e:
        raise HTTPException(status_code=502, detail=_safe_http_detail("vector get", e))
    if not points:
        raise HTTPException(status_code=404, detail=f"contradiction '{contra_id}' not found")
    meta = points[0].payload or {}
    new_id = meta.get("new_id")
    old_id = meta.get("old_id")

    if req.action == "keep_new" and old_id:
        try:
            store.delete(sem_col, ids=[old_id])
        except Exception as e:
            log.warning("contradiction_resolution_error", phase="delete_old", error=str(e))
        # Mark winner as superseding loser
        try:
            store.update_payload(sem_col, ids=[new_id], patch={"supersedes": old_id})
        except Exception as e:
            log.warning("contradiction_resolution_error", phase="supersede", error=str(e))
        # Phase G2: winner is no longer provisional — make it visible to recall.
        if new_id:
            try:
                from brain_core.atoms_store import update_provisional_flag

                update_provisional_flag(new_id, False)
            except Exception:
                pass
    elif req.action == "keep_old" and new_id:
        try:
            store.delete(sem_col, ids=[new_id])
        except Exception as e:
            log.warning("contradiction_resolution_error", phase="delete_new", error=str(e))
    elif req.action == "merge" and old_id and new_id:
        # Combine both entries: keep old ID, merge content
        try:
            both = store.get(
                sem_col,
                ids=[old_id, new_id],
                with_payload=True,
                with_documents=True,
                with_vectors=False,
            )
            by_id = {p.id: p for p in both}
            old_p = by_id.get(old_id)
            new_p = by_id.get(new_id)
            if old_p and new_p and old_p.document and new_p.document:
                merged = (old_p.document.strip() + "\n\n" + new_p.document.strip())[:1000]
                merged_payload = dict(old_p.payload or {})
                # Re-embed merged content so vector search stays accurate
                try:
                    new_emb = _get_embedding(merged, use_cache=False, prefix="passage")
                    store.upsert(
                        sem_col,
                        ids=[old_id],
                        vectors=[new_emb],
                        documents=[merged],
                        payloads=[merged_payload],
                    )
                except Exception as e:
                    log.warning("contradiction_resolution_error", error=str(e))
                    # Fall back to metadata-only merge — keep the old vector
                    # since re-embed failed; content patch is best-effort.
                    store.update_payload(
                        sem_col,
                        ids=[old_id],
                        patch={"merged_content": merged},
                    )
                store.delete(sem_col, ids=[new_id])
        except Exception as e:
            log.warning("contradiction_resolution_error", error=str(e))
            raise HTTPException(status_code=500, detail=f"resolution failed: {e}")
    # both_true / dismiss: leave both entries, just resolve the contradiction record
    if req.action in ("both_true", "dismiss") and new_id:
        # Phase G2: contradiction declared not blocking — clear the provisional
        # gate on the new atom so search_unified surfaces it again.
        try:
            from brain_core.atoms_store import update_provisional_flag

            update_provisional_flag(new_id, False)
        except Exception:
            pass

    # Audit trail
    try:
        from audit_log import log_event

        log_event(
            event_type="resolve",
            entity_a=old_id or "",
            entity_b=new_id or "",
            conflict_type="contradiction",
            resolution=req.action,
            reason=f"User resolved contradiction {contra_id}",
            source_evidence={"old_id": old_id, "new_id": new_id},
        )
    except Exception as e:
        log.warning("contradiction_resolution_error", error=str(e))

    # Mark contradiction resolved (delete from queue)
    try:
        store.delete(contra_col, ids=[contra_id])
    except Exception as e:
        log.warning("contradiction_resolution_error", error=str(e))

    return {"status": "resolved", "id": contra_id, "action": req.action}


# ── Routes: reasoning + decision ── moved to brain_core/routes/decide.py


# /brain/proactive + /brain/insights moved to brain_core/routes/insights.py


# ── autonomy + focus + D1 messaging ── moved to brain_core/routes/agency.py


# ── Phase D3: Contradiction voting ──
class ContradictionVoteRequest(BaseModel):
    voter_agent: str = Field(..., max_length=32)
    vote: Literal["keep_new", "keep_old", "merge", "dismiss"]
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    reasoning: str = Field(default="", max_length=500)


@router.post("/memory/contradictions/{contra_id}/vote", tags=["memory"])
def vote_on_contradiction(contra_id: Annotated[str, PathParam()], req: ContradictionVoteRequest) -> dict:
    """Cast an agent vote on how to resolve a contradiction."""
    try:
        from datetime import datetime as _dt

        with _votes_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO contradiction_votes (contradiction_id, voter_agent, vote, confidence, reasoning, voted_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    contra_id,
                    req.voter_agent,
                    req.vote,
                    req.confidence,
                    req.reasoning,
                    _dt.now(UTC).isoformat(),
                ),
            )
            conn.commit()
            rows = conn.execute(
                "SELECT vote, COUNT(*) FROM contradiction_votes WHERE contradiction_id=? GROUP BY vote",
                (contra_id,),
            ).fetchall()
        tally = {vote: count for vote, count in rows}
        total = sum(tally.values())
        return {
            "contradiction_id": contra_id,
            "voter": req.voter_agent,
            "vote": req.vote,
            "tally": tally,
            "total_votes": total,
            "consensus_reached": total >= 3 and max(tally.values()) >= 2,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e))


@router.get("/brain/conflict-resolution-report", tags=["memory"])
def conflict_resolution_report(limit: int = 50) -> dict:
    """Pending contradiction queue with deterministic policy recommendations."""
    listed = list_contradictions(limit=limit)
    by_action: dict[str, int] = {}
    review_required = 0
    auto_apply = 0
    for item in listed.results:
        rec = item.recommendation or {}
        action = str(rec.get("action") or "unknown")
        by_action[action] = by_action.get(action, 0) + 1
        if rec.get("review_required"):
            review_required += 1
        if rec.get("auto_apply"):
            auto_apply += 1
    return {
        "total": listed.total,
        "returned": len(listed.results),
        "by_recommended_action": by_action,
        "auto_apply_candidates": auto_apply,
        "review_required": review_required,
        "items": [item.model_dump() for item in listed.results],
    }


@router.get("/memory/contradictions/{contra_id}/votes", tags=["memory"])
def get_contradiction_votes(contra_id: Annotated[str, PathParam()]) -> dict:
    """List all votes for a contradiction."""
    try:
        with _votes_conn() as conn:
            rows = conn.execute(
                "SELECT voter_agent, vote, confidence, reasoning, voted_at "
                "FROM contradiction_votes WHERE contradiction_id=? ORDER BY voted_at",
                (contra_id,),
            ).fetchall()
            tally_rows = conn.execute(
                "SELECT vote, COUNT(*) FROM contradiction_votes WHERE contradiction_id=? GROUP BY vote",
                (contra_id,),
            ).fetchall()
        votes = [
            {"voter_agent": r[0], "vote": r[1], "confidence": r[2], "reasoning": r[3], "voted_at": r[4]}
            for r in rows
        ]
        tally = {v: c for v, c in tally_rows}
        return {
            "contradiction_id": contra_id,
            "total_votes": len(votes),
            "tally": tally,
            "votes": votes,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e))


# ── Phase D4: session_active_agents ── moved to brain_core/routes/ops.py


# triggers + B1-B4 moved to brain_core/routes/agency.py


# ── Phase M6: SearXNG web search ── moved to brain_core/routes/web.py


# ── Phase B5: atoms ── moved to brain_core/routes/agency.py


# ── SLO + trace + ingest + index + canonical_lint + canonicalize + answer_candidates ── moved to brain_core/routes/knowledge.py


# ── Routes: audit log ── moved to brain_core/routes/admin_ops.py (see /brain/audit* endpoints)


# ── Phase 5 autonomy gate + Phase 4 SM-2 ── moved to brain_core/routes/governance.py


# /brain/audit/stats + /brain/audit/{event_id}/review moved to brain_core/routes/admin_ops.py


# ── facts, graph, lessons, claude-session ── moved to brain_core/routes/stores.py


# claude-session info + claude-queue moved to brain_core/routes/governance.py


# ── Valence / attention / predictive / usage ── moved to brain_core/routes/brain_ops.py


@router.get("/brain/timetravel", tags=["brain"])
def timetravel(
    date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    q: str = Query(default="", max_length=500),
    limit: int = Query(default=10, ge=1, le=100),
) -> dict:
    """Time-travel query: replay brain state as it was on date X.

    Uses Phase 1C temporal validity (valid_from/valid_until) to filter memories
    that were valid on the given date. Useful for debugging 'what did the brain
    know about X on date Y?'.
    """
    try:
        if q:
            # Search with as_of filter
            payload = search_unified.search_all(
                q,
                limit,
                sources=["rag", "canonical"],
                include_history=True,  # include superseded for historical accuracy
                include_obsolete=True,
                as_of=date,
                # F6: historical queries need all hygiene filters off too
                include_provisional=True,
                include_all_speakers=True,
                include_session_scope=True,
                include_low_trust=True,
                include_expired=True,
            )
            return {
                "date": date,
                "query": q,
                "total": len(payload.get("results", [])),
                "results": payload.get("results", [])[:limit],
            }
        # No query — summarize: count memories by class that existed on date
        collection = _memory_collection_id()
        # Fetch all memories, filter by temporal validity
        points = get_vector_store().get(
            collection,
            limit=10000,
            with_payload=True,
            with_documents=False,
        )
        metas = [p.payload or {} for p in points]

        as_of_date = date[:10]
        valid_count = 0
        by_class: dict[str, int] = {}
        by_category: dict[str, int] = {}

        for meta in metas:
            meta = meta or {}
            vf = (meta.get("valid_from", "") or "")[:10]
            vu = (meta.get("valid_until", "") or "")[:10]
            if vf and vf > as_of_date:
                continue
            if vu and vu <= as_of_date:
                continue
            valid_count += 1
            mc = meta.get("memory_class", "unknown")
            by_class[mc] = by_class.get(mc, 0) + 1
            cat = meta.get("category", "unknown")
            by_category[cat] = by_category.get(cat, 0) + 1

        return {
            "date": date,
            "total_valid_memories": valid_count,
            "by_memory_class": by_class,
            "by_category": by_category,
            "total_all_time": len(metas),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e))


# /brain/changes + /brain/evolution moved to brain_core/routes/governance.py


# ── Phase E1: Session context API ──
# _session_conn + E1/E2/E4/F1/code/tools/accuracy/outcomes/procedures moved to brain_core/routes/ops.py


# ── observability + schema + self-heal + admin ── moved to brain_core/routes/health.py
