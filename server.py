#!/Users/chrischo/server/brain/.venv/bin/python
"""brain.server — FastAPI long-running brain API.

The single source of truth for read + capture access to the second brain.
Holds search_unified, search, search_memory, temporal, boot_context, learn,
and the profile cache in memory (no per-cron Python cold start).

Auto-generated OpenAPI docs at GET /docs (Swagger) and GET /redoc.

Run via: /Users/chrischo/server/brain/.venv/bin/python /Users/chrischo/server/brain/server.py
or:      /Users/chrischo/server/brain/.venv/bin/uvicorn server:app
            --host 127.0.0.1 --port 8791
"""

import hashlib
import hmac
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import structlog
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
)
from fastapi import (
    Path as PathParam,
)

log = structlog.get_logger("brain.server")
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

# In-process modules — brain_core is the single source of truth.
_BRAIN_CORE = str(Path(__file__).parent / "brain_core")
sys.path.insert(0, _BRAIN_CORE)
import active_recall  # noqa: E402  — v3 thalamus / per-turn attention
import boot_context  # noqa: E402
import hyde as _hyde  # noqa: E402
import learn  # noqa: E402
import rerank as _rerank  # noqa: E402
import rrf as _rrf  # noqa: E402
import search_unified  # noqa: E402
import temporal  # noqa: E402
import time_decay as _time_decay  # noqa: E402
from metrics_buffer import metrics_buffer as _metrics_buf  # noqa: E402
from openclaw_dispatch import dispatch as _openclaw_dispatch  # noqa: E402

# 2026-04-17 — first-failure flag so hook telemetry bugs surface once in logs
# instead of being silently swallowed by bare `except: pass` on every request.
_hook_metrics_warned = False
# ── Config ──────────────────────────────────────────────
from config import (  # noqa: E402
    BRAIN_DIR,
    DISTILLED_DAILY,
    FAILURE_LOG,
    IDENTITY_FILE,
    INBOX_DIR,
    MONTHLY_DIR,
    PYTHON,
    SECRET_FILE,
    STATE_FILE,
    WEEKLY_DIR,
)
from indexer import (
    get_embedding as _get_embedding,
)
from vector_store import get_vector_store  # noqa: E402
from scheduler import brain_scheduler  # noqa: E402
from job_registry import JOB_REGISTRY, dispatch_job  # noqa: E402 F401
from api_deps import (  # noqa: E402
    LISTEN_HOST,
    LISTEN_PORT,
    SERVER_START,
    HealthResponse,
    _current_secret,
    _load_secret,
    _log_failure,
    _safe_http_detail,
    prime_secret_cache,
    verify_bearer,
)

PROFILE_CACHE_TTL = 60

# JOB_REGISTRY moved to brain_core/job_registry.py

# _running_jobs + _CRITICAL_JOBS moved to brain_core/job_registry.py

# ── Pydantic models ─────────────────────────────────────
# MetricsResponse moved to brain_core/routes/metrics.py


# CaptureRequest/CaptureResponse moved to brain_core/routes/capture.py


class JobResponse(BaseModel):
    status: Literal["queued"] = "queued"
    job: str
    pid: int


class RecallResultMetadata(BaseModel):
    agent: str | None = None
    service: str | None = None
    type: str | None = None
    domain: str | None = None
    confidence: float | None = None
    review_state: str | None = None
    vector_score: float | None = None
    keyword_score: float | None = None
    id: str | None = None


class RecallResult(BaseModel):
    model_config = {"extra": "allow"}  # tolerate extra fields like rrf_score, provenance
    score: float
    source_type: str = ""  # graph results use "entity"; rag results may omit
    collection: str = ""
    title: str = ""
    content: str = ""
    path: str = ""
    trust_tier: int = 1
    metadata: dict[str, Any] = Field(default_factory=dict)


class RecallResponse(BaseModel):
    query: str
    results: list[RecallResult]
    sources_searched: list[str]
    total_candidates: int
    temporal_range: dict | None = None
    expanded_query: str | None = None


class RecallV2Response(BaseModel):
    query: str
    results: list[dict[str, Any]]
    total_candidates: int
    hyde_used: bool = False
    hypothetical: str | None = None
    variants: list[str] = Field(default_factory=list)
    rerank_applied: bool = True
    time_decay_applied: bool = True
    latency_ms: int = 0
    timing: dict[str, Any] = Field(default_factory=dict)
    # 2026-04-17 Phase 4: proactive metacognitive note. Populated only
    # when the top-1 result triggers an uncertainty heuristic (low
    # confidence, pending contradictions, tied top-K, no trusted
    # alternatives). None / absent when the brain is confident — keeps
    # high-trust recall responses clean.
    meta_note: str | None = None


class RecallActiveRequest(BaseModel):
    """Per-turn active recall payload. POSTed by claude_boot.sh and OpenClaw
    before_prompt_build plugin on every user turn."""

    prompt: str = Field(..., max_length=8000)
    session_id: str = Field(default="anon", max_length=128)
    turn_idx: int = Field(default=0, ge=0, le=100000)
    agent: str = Field(default="claude", max_length=32)
    cwd: str | None = Field(default=None, max_length=512)
    seen_hashes: list[str] | None = Field(default=None, max_length=200)


class InjectionBlockModel(BaseModel):
    id: str
    title: str
    content: str
    source: str
    score: float
    priority: str
    path: str | None = None


class RecallActiveResponse(BaseModel):
    blocks: list[InjectionBlockModel] = Field(default_factory=list)
    intent: str | None = None
    total_tokens: int = 0
    latency_ms: int = 0
    new_since_last_turn: bool = False
    degraded: bool = False


# ImageIngestRequest moved to brain_core/routes/ingest.py

# WorkingMemorySetRequest/Item moved to brain_core/routes/wm.py


class SearchFeedbackRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    result_id: str = Field(..., min_length=1, max_length=200)
    result_source: str = Field(default="", max_length=64)
    useful: bool
    # Forward-compat: agent identity for per-agent preference learning.
    # Pre-2026-04 entries lack this field and are treated as agent="system"
    # by feedback_aggregator.
    agent: str = Field(default="system", max_length=32)
    # Phase 7: eval auto-growth signal. When wrong_answer=true and `expected`
    # is set, the query is appended to eval_proposals for weekly review.
    wrong_answer: bool = Field(default=False)
    expected: str = Field(default="", max_length=2000)


# Think* models moved to brain_core/routes/think.py


# ── Decision / reasoning models ─────────────────────────
# Decide/Reason models moved to brain_core/routes/decide.py


# ── Autonomy models ────────────────────────────────────
# Autonomy pydantic models moved to brain_core/routes/agency.py


# ── Self-learning + memory CRUD models ─────────────────
# LearnRequest / LearnResponse moved to brain_core/routes/learn.py


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


class ContradictionListResponse(BaseModel):
    results: list[ContradictionEntry]
    total: int


class ContradictionResolveRequest(BaseModel):
    action: Literal["keep_new", "keep_old", "both_true", "merge", "dismiss"]


# BrainIngestRequest moved to brain_core/routes/knowledge.py


# ── Caches ──────────────────────────────────────────────
from profile_cache import profile_cache as _profile_cache  # noqa: E402


# ── Helpers ─────────────────────────────────────────────
# _get_collection_counts moved to brain_core/routes/metrics.py


# _build_raw_record + _write_inbox moved to brain_core/routes/capture.py


# ── Auth dependency ── moved to brain_core/api_deps.py (verify_bearer imported above)


# ── App ─────────────────────────────────────────────────
def _prewarm_caches() -> None:
    """Pre-warm embedding + HyDE caches with common queries on startup.

    This eliminates the 15-16s cold-start on the first /recall/v2?hyde=true call
    by front-loading the Ollama embed + Jenna HyDE dispatch before any user
    request hits. Runs in a background thread so it doesn't block uvicorn startup.
    """
    import threading

    PREWARM_QUERIES = [
        "chris preference frontend stack",
        "openclaw gateway config",
        "docker nginx setup",
        "brain api self-learning",
        "what does chris prefer",
        "homelab infrastructure",
        "recent decisions",
        "calendar schedule this week",
        "jenna agent workflow",
        "conventional commits git",
    ]

    try:
        from boot_context import _predictive_queries

        PREWARM_QUERIES.extend(_predictive_queries("claude"))
    except Exception:
        pass

    def _warm():
        # Warm the embedding cache (fast, ~50ms each) + collections cache.
        # HyDE warm-up is skipped — each Jenna dispatch takes 10-15s and would
        # block user requests if they race for the same OpenClaw session.
        try:
            from search import get_collections, get_embedding

            get_collections()  # populate the collections name→id cache
            for q in PREWARM_QUERIES:
                get_embedding(q)
        except Exception:
            pass

    t = threading.Thread(target=_warm, daemon=True, name="prewarm")
    t.start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the in-process scheduler + pre-warm caches on boot."""
    # Configure structured JSON logging
    logging.basicConfig(format="%(message)s", level=logging.INFO)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=32))
    # Phase A6: run schema migrations before starting scheduler
    try:
        from brain_core.schema_versions import check_and_migrate

        migration_result = check_and_migrate()
        log.info("schema_migrations", **migration_result)
    except RuntimeError as e:
        # Downgrade refused — halt startup rather than running against stale code.
        sys.stderr.write(f"FATAL: schema migration refused: {e}\n")
        raise
    except Exception as e:
        log.warning("schema_migration_failed", error=str(e))
    try:
        brain_scheduler.start(dispatch_job)
    except Exception as e:
        _log_failure(f"scheduler start failed: {e}", route="lifespan")

    # Periodic metrics snapshot persistence so SLO reader always sees fresh
    # route/phase latency data. Without this the only snapshot is the one
    # written on shutdown — a cold-boot row with 0-7 samples was poisoning
    # recall_v2_p95_ms for 9+ hours after each restart.
    def _persist_metrics_snapshot() -> None:
        try:
            _metrics_buf.persist_to_sqlite(str(BRAIN_DIR / "logs" / "metrics_history.db"))
        except Exception as e:
            log.warning("metrics_persist_failed", error=str(e))

    try:
        brain_scheduler.schedule_inprocess(
            _persist_metrics_snapshot,
            name="metrics_persist",
            seconds=300,
            description="Persist metrics_buf snapshot every 5 min (for SLO reader)",
        )
    except Exception as e:
        log.warning("metrics_persist_register_failed", error=str(e))
    prime_secret_cache()
    _prewarm_caches()
    # Start the brain_loop wake watcher so /tmp/.brain_loop_wake file touches
    # fire a tick subprocess within ~50ms. attention.enqueue + coding_events
    # outcome writes touch that file on important events. Without this the
    # watcher daemon never starts (brain_loop_tick runs as an ephemeral
    # subprocess, so an in-process-only watcher wouldn't persist).
    try:
        from brain_core.brain_loop import _ensure_wake_watcher as _start_wake_watcher

        _start_wake_watcher()
        log.info("brain_loop_wake_watcher_started")
    except Exception as e:
        log.warning("wake_watcher_start_failed", error=str(e))
    # Warm the real cross-encoder (BGE-reranker-base) if enabled so the first
    # /recall/v2 call doesn't eat the 2-5s cold model load. Runs in a background
    # thread so startup doesn't block on model download.
    try:
        from brain_core import config as _brain_config

        if getattr(_brain_config, "BRAIN_CROSS_ENCODER_ENABLED", False):
            import threading

            def _warm_ce():
                try:
                    from brain_core.cross_encoder_model import warmup as _ce_warmup

                    ok = _ce_warmup()
                    log.info("cross_encoder_warmup", ok=ok)
                except Exception as _e:
                    log.warning("cross_encoder_warmup_failed", error=str(_e))

            threading.Thread(target=_warm_ce, daemon=True).start()
    except Exception:
        pass
    _metrics_buf.load_from_sqlite(str(BRAIN_DIR / "logs" / "metrics_history.db"))
    yield
    _metrics_buf.persist_to_sqlite(str(BRAIN_DIR / "logs" / "metrics_history.db"))
    try:
        brain_scheduler.shutdown()
    except Exception:
        pass


app = FastAPI(
    title="Chris Brain API",
    description="Long-running second-brain HTTP API. In-process search, scheduled jobs, schema-validated capture, self-learning.",
    version="2.1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# ── Phase M5: per-route rate limiting via slowapi ─────────
# Defends against token-leak runaway (hardest gap in the commercial-bar audit:
# /learn dispatches openclaw LLM calls; an unbounded loop bills real money).
# Disable in tests via BRAIN_RATE_LIMIT_DISABLED=1.
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from rate_limit import limiter  # shared instance for route modules

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)


# 2026-04-17: generic exception handler. Prevents internal details from
# escaping via HTTP responses on unexpected crashes. Explicit
# HTTPException(detail=...) raises are still serialized as-is (FastAPI
# routes them before this handler fires) — so individual endpoints that
# carefully craft user-friendly messages keep working.
@app.exception_handler(Exception)
async def _generic_exception_handler(request, exc):  # type: ignore[no-untyped-def]
    import uuid

    from fastapi.responses import JSONResponse

    err_id = uuid.uuid4().hex[:12]
    try:
        log.exception("unhandled exception err_id=%s path=%s", err_id, request.url.path)
    except Exception:
        pass
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal error",
            "err_id": err_id,
        },
    )


_cors_origins = os.getenv("BRAIN_CORS_ORIGINS", "").strip()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins.split(",")
    if _cors_origins
    else [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8791",
        "http://127.0.0.1:8791",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)


# ── Request ID + latency middleware ─────────────────────
# 2026-04-16 Tier 2 fix: correlation-ID propagation. Previously concurrent
# recalls interleaved in structlog output with no way to trace a single
# request through the pipeline. Now every request gets a ULID-style
# rid=<12 hex chars> bound to the structlog context for the duration of
# the request and echoed back in the X-Request-ID header so callers
# (brain-ui, Claude hooks, Jenna) can surface it on failures.
import contextvars as _contextvars
import secrets as _secrets

_request_id_ctx: _contextvars.ContextVar[str] = _contextvars.ContextVar("brain_request_id", default="")


def get_request_id() -> str:
    """Return the current request's correlation ID (empty string outside a request)."""
    return _request_id_ctx.get()


@app.middleware("http")
async def _request_id_and_metrics_middleware(request, call_next):
    # Allow callers to pass their own correlation ID (e.g. Claude hooks
    # chaining calls); generate a fresh one otherwise.
    rid_in = request.headers.get("x-request-id", "").strip()
    rid = rid_in or _secrets.token_hex(6)
    token = _request_id_ctx.set(rid)
    # Bind to structlog for the duration of this request.
    _log_vars = structlog.contextvars.bind_contextvars(request_id=rid)
    t0 = time.time()
    error = False
    status_code = 0
    try:
        response = await call_next(request)
        status_code = response.status_code
        if response.status_code >= 500:
            error = True
        response.headers["X-Request-ID"] = rid
        return response
    except Exception:
        error = True
        status_code = 500
        raise
    finally:
        latency_ms = (time.time() - t0) * 1000
        # 2026-04-16 R-8: record status code alongside latency so the
        # metrics buffer can distinguish 4xx from 5xx after the fact.
        # Falls back to positional call when the buffer hasn't been
        # migrated yet — forward-compatible with older buffers.
        try:
            _metrics_buf.record_request(
                str(request.url.path), latency_ms, error=error, status_code=status_code
            )
        except TypeError:
            _metrics_buf.record_request(str(request.url.path), latency_ms, error=error)
        structlog.contextvars.unbind_contextvars("request_id")
        _request_id_ctx.reset(token)


# ── Routes: liveness ── moved to brain_core/routes/liveness.py (include_router below)

# ── Routes: metrics ── moved to brain_core/routes/metrics.py


# ── Routes: profile ── moved to brain_core/routes/profile.py


# ── Routes: recall ──────────────────────────────────────
@app.get("/recall", response_model=RecallResponse, tags=["recall"], dependencies=[Depends(verify_bearer)])
@limiter.limit("3000/minute")  # M7-WS7 + M8 follow-up: read path — same envelope as /recall/v2
def recall(
    request: Request,
    q: str,
    n: int = Query(default=10, ge=1, le=50),
    since: str | None = None,
    until: str | None = None,
    entity: str | None = None,
    collection: str | None = None,
    domain: str | None = None,
    source_type: str | None = Query(default=None, max_length=32),
    include_history: bool = Query(default=False),
    include_obsolete: bool = Query(default=False),
    as_of: str | None = Query(default=None, max_length=20),
) -> dict:
    """Multi-dimensional in-process search across rag + canonical + obsidian.

    Phase 1 filters:
      include_history — show superseded memories (default: hide)
      include_obsolete — show obsolete tier memories (default: hide)
      as_of=YYYY-MM-DD — temporal replay: memories valid at that date
    """
    if not q.strip():
        raise HTTPException(status_code=400, detail="q parameter required")

    # Semantic similarity cache — only for plain queries (no filters)
    # When filters are present, results differ per filter combo so we skip cache.
    _filter_free = not any(
        (since, until, entity, collection, domain, source_type, include_history, include_obsolete, as_of)
    )
    if _filter_free:
        cached = _recall_emb_cache_lookup(q)
        if cached is not None:
            # Round 10 C1: still reinforce semantic_memory hits even on cache
            # hit — the user is "accessing" those memories regardless of where
            # the response comes from. Fire-and-forget so cache lookups stay fast.
            try:
                cached_results = cached.get("results", []) if isinstance(cached, dict) else []
                if cached_results:
                    from brain_core.memory_lifecycle import reinforce_all_collections
                    from brain_core.search_unified import _search_bg_pool

                    _search_bg_pool.submit(reinforce_all_collections, cached_results)
            except Exception:
                pass
            return cached

    start_dt, end_dt = temporal.parse_range(since, until)
    # ChromaDB 1.4.1 rejects string operands in $gte/$lt; filter Python-side instead.
    where = None
    collections_arg = [collection] if collection else None
    # Widen n when a temporal filter will post-drop rows so we still return ~n.
    search_n = n * 3 if (start_dt or end_dt) else n

    payload = search_unified.search_all(
        q,
        search_n,
        sources=["rag", "canonical", "obsidian"],
        domain=domain,
        original_query=q,
        where=where,
        collections=collections_arg,
        entity=entity,
        explain=False,
        source_type=source_type,
        include_history=include_history,
        include_obsolete=include_obsolete,
        as_of=as_of,
    )
    if (start_dt or end_dt) and isinstance(payload, dict):
        payload["results"] = temporal.filter_by_created_at(payload.get("results", []), start_dt, end_dt)[:n]
    if _filter_free:
        _recall_emb_cache_put(q, payload)

    # Gap logging moved to /recall/v2 handler (2026-04-12): v2 is the hot path
    # (2400+ requests/day vs v1's ~1800, most of v1 are test-harness) and the
    # v1 threshold of max_score<5.0 never fired in practice — scores are clipped
    # to [0,100] with typical relevant hits at 30-80.

    # Round 10 C1: reinforce-on-access (MemoryBank). Fire-and-forget so we
    # don't add latency to /recall. Only reinforces semantic_memory hits in
    # the top-N — they're the only collection with the access_count metadata.
    # The id may live at top-level (rag results) or nested under metadata.id
    # (canonical results) so we check both paths.
    try:
        results_list = payload.get("results", []) if isinstance(payload, dict) else []
        if results_list:
            from brain_core.memory_lifecycle import reinforce_all_collections
            from brain_core.search_unified import _search_bg_pool

            _search_bg_pool.submit(reinforce_all_collections, results_list)
    except Exception:
        pass
    return payload


# ── Recall v2 response cache (30s TTL) ──
_recall_cache: dict[str, tuple[float, RecallV2Response]] = {}
_recall_cache_lock = threading.Lock()
_RECALL_CACHE_TTL = 30.0
_RECALL_CACHE_MAX = 100
# Separate lock for the semantic-similarity embedding cache. Sharing the
# response-cache lock meant the cosine scan (O(N*dim)) ran under a contention
# hotspot — every concurrent recall/v2 caller serialized on it.
_recall_emb_lock = threading.Lock()


def _recall_cache_get(key: str) -> RecallV2Response | None:
    with _recall_cache_lock:
        entry = _recall_cache.get(key)
        if entry and (time.time() - entry[0]) < _RECALL_CACHE_TTL:
            return entry[1]
        if entry:
            del _recall_cache[key]
    return None


def _recall_cache_put(key: str, response: RecallV2Response) -> None:
    with _recall_cache_lock:
        _recall_cache[key] = (time.time(), response)
        if len(_recall_cache) > _RECALL_CACHE_MAX:
            oldest = min(_recall_cache, key=lambda k: _recall_cache[k][0])
            del _recall_cache[oldest]


# ── Semantic query cache for /recall (embedding-similarity based, 60s TTL) ──
_recall_embedding_cache: list[
    tuple[float, list[float], str, dict]
] = []  # (timestamp, embedding, query, response)
_RECALL_EMB_TTL = 60.0
_RECALL_EMB_MAX = 50
_RECALL_EMB_SIM_THRESHOLD = 0.92

# 2026-04-16 Tier 2: Matryoshka-style dimension truncation for the recall
# semantic-similarity cache. multilingual-e5-large-instruct emits 1024-dim
# vectors, and the cache's linear scan (~50 entries × 1024 dims per miss)
# paid ~2ms of pure Python cosine work per request on top of the ~60ms
# Ollama embed. Matryoshka Representation Learning (Kusupati 2022) shows
# that truncating an embedding to its first k dimensions + re-normalizing
# preserves near-full retrieval quality at a fraction of the compute.
# 256 dims = 4× faster cosine, measured ≤2% recall loss in literature.
# The threshold is unchanged because cosine on L2-normalized prefixes
# stays comparable to full-vector cosine.
_MATRYOSHKA_DIM = 256


def _truncate_normalize(vec: list[float], dim: int = _MATRYOSHKA_DIM) -> list[float]:
    import math

    if not vec or len(vec) <= dim:
        return vec
    head = vec[:dim]
    norm = math.sqrt(sum(x * x for x in head))
    if norm <= 0:
        return head
    return [x / norm for x in head]


def _cosine(a: list[float], b: list[float]) -> float:
    import math

    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _recall_emb_cache_lookup(query: str) -> dict | None:
    """Check semantic similarity cache. Returns cached response or None."""
    if not query:
        return None
    try:
        emb = _get_embedding(query[:200], use_cache=True, prefix="query")
    except Exception:
        return None
    if not emb:
        return None
    # Truncate + renormalize ONCE per lookup — the cached entries are
    # already stored in their truncated form.
    emb_trunc = _truncate_normalize(emb)
    now = time.time()
    # Snapshot under lock, scan outside. The cosine loop is O(N*dim) ~50k
    # float mults and must not run inside a contention hotspot.
    with _recall_emb_lock:
        _recall_embedding_cache[:] = [e for e in _recall_embedding_cache if now - e[0] < _RECALL_EMB_TTL]
        snapshot = list(_recall_embedding_cache)
    for _ts, cached_emb, _cached_query, resp in snapshot:
        if _cosine(emb_trunc, cached_emb) > _RECALL_EMB_SIM_THRESHOLD:
            return resp
    return None


def _recall_emb_cache_put(query: str, response: dict) -> None:
    if not query:
        return
    try:
        emb = _get_embedding(query[:200], use_cache=True, prefix="query")
    except Exception:
        return
    if not emb:
        return
    # Store only the truncated + renormalized prefix to match lookup-side.
    emb_trunc = _truncate_normalize(emb)
    now = time.time()
    with _recall_emb_lock:
        # 2026-04-16 R-4: prune by TTL at put time, not just at lookup.
        # Previously lookup-only eviction let expired entries accumulate
        # when reads were sparse, wasting the 50-slot budget and evicting
        # still-valid entries prematurely.
        _recall_embedding_cache[:] = [e for e in _recall_embedding_cache if now - e[0] < _RECALL_EMB_TTL]
        _recall_embedding_cache.append((now, emb_trunc, query, response))
        if len(_recall_embedding_cache) > _RECALL_EMB_MAX:
            _recall_embedding_cache.pop(0)


# ── Routes: recall v2 (HyDE + expand + rerank + time-decay + RRF) ──
_auto_feedback_count = 0
_auto_feedback_hour = 0  # hour (unix ts // 3600) of last reset
_AUTO_FEEDBACK_MAX_PER_HOUR = 100


def _build_meta_note(top_results: list[dict]) -> str | None:
    """Compose a proactive metacognitive note when the top-1 result has
    signals of uncertainty. Heuristic only — no LLM call, fires in <1ms.

    Triggers (any):
      1. Calibrated confidence < 0.5 on top-1
      2. pending_contradictions > 0 on top-1
      3. Top-2 scores within 5% — ambiguous winner
      4. trust_tier == 0 on top-1 AND every other result <40 score

    Multiple triggers combine with " · " separator. Returns None when no
    trigger fires so high-confidence queries stay clean.
    """
    if not top_results:
        return None
    top1 = top_results[0] if isinstance(top_results[0], dict) else None
    if top1 is None:
        return None
    notes: list[str] = []

    # 1. Low calibrated confidence
    try:
        conf = float(top1.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    if conf and conf < 0.5:
        notes.append(f"⚠ Low confidence ({conf:.2f}) — verify before acting")

    # 2. Pending contradictions
    try:
        pc = int(top1.get("pending_contradictions") or 0)
    except (TypeError, ValueError):
        pc = 0
    if pc > 0:
        plural = "s" if pc > 1 else ""
        notes.append(f"⚠ Top result has {pc} open contradiction{plural} — call brain_doubt for both sides")

    # 3. Ambiguous top-2
    if len(top_results) >= 2 and isinstance(top_results[1], dict):
        try:
            s1 = float(top1.get("score") or 0)
            s2 = float(top_results[1].get("score") or 0)
            if s1 > 0 and (s1 - s2) / s1 < 0.05:
                notes.append(f"⚠ Ambiguous: top-2 scores within {((s1-s2)/s1)*100:.1f}%")
        except (TypeError, ValueError):
            pass

    # 4. Untrusted top-1 with weak alternatives
    try:
        top1_trust = int(top1.get("trust_tier") or 0)
        top1_score = float(top1.get("score") or 0)
    except (TypeError, ValueError):
        top1_trust, top1_score = 0, 0.0
    if top1_trust == 0 and top1_score > 40:
        others_weak = all(
            float((r or {}).get("score") or 0) < 40 for r in top_results[1:4] if isinstance(r, dict)
        )
        if others_weak:
            notes.append("⚠ No high-trust match — top result is untiered")

    if not notes:
        return None
    return " · ".join(notes)


def _record_auto_feedback(query: str, results: list[dict], agent: str) -> None:
    """Log served-result impressions. Rate-limited.

    2026-04-16 fix: this function used to auto-reinforce every served
    semantic_memory hit (write score=0.7 + fire reinforce_on_access).
    That created a rich-get-richer spiral — Bjork's interference theory
    predicts frequently-retrieved items should dominate further retrieval
    only when they're actually useful, not merely served. Now:
      - impressions are logged as served-without-score (for LtR training)
      - reinforcement is gated to EXPLICIT /recall/feedback signals only
    Net: salience.access_count only bumps on confirmed usefulness.
    """
    global _auto_feedback_count, _auto_feedback_hour
    now = datetime.now(UTC)
    current_hour = int(now.timestamp()) // 3600
    if current_hour != _auto_feedback_hour:
        _auto_feedback_count = 0
        _auto_feedback_hour = current_hour
    if _auto_feedback_count >= _AUTO_FEEDBACK_MAX_PER_HOUR:
        return
    feedback_log = BRAIN_DIR / "logs" / "search-feedback.jsonl"
    feedback_log.parent.mkdir(parents=True, exist_ok=True)
    ts = now.isoformat()
    lines: list[str] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        rid = r.get("id") or r.get("path") or (r.get("metadata") or {}).get("id") or ""
        col = r.get("collection") or ""
        lines.append(
            json.dumps(
                {
                    "query": query[:500],
                    "result_id": rid,
                    "result_source": col,
                    # score=None marks this as an impression, not a reward.
                    # The learning-to-rank pipeline treats impression-only
                    # as an unlabeled observation — does not update trust.
                    "score": None,
                    "served": True,
                    "timestamp": ts,
                    "agent": agent,
                }
            )
        )
    if not lines:
        return
    budget = _AUTO_FEEDBACK_MAX_PER_HOUR - _auto_feedback_count
    lines = lines[:budget]
    try:
        with feedback_log.open("a") as f:
            f.write("\n".join(lines) + "\n")
        _auto_feedback_count += len(lines)
    except Exception:
        pass
    # Reinforcement REMOVED from the served path (see docstring).
    # Explicit reinforcement still happens in POST /recall/feedback.


@app.get(
    "/recall/v2", response_model=RecallV2Response, tags=["recall"], dependencies=[Depends(verify_bearer)]
)
@limiter.limit("3000/minute")  # M7-WS7 + M8 follow-up: read path is non-LLM-billable (Ollama only).
# Bumped from 600 → 3000 because back-to-back eval (1212 calls/run) was burst-throttling.
def recall_v2(
    request: Request,
    q: str,
    n: int = Query(default=10, ge=1, le=50),
    hyde: bool = False,
    expand: bool = False,
    rerank: bool = True,
    decay: bool = True,
    iterative: bool = False,
    since: str | None = None,
    until: str | None = None,
    entity: str | None = None,
    collection: str | None = None,
    domain: str | None = None,
    source_type: str | None = Query(default=None, max_length=32),
    include_history: bool = Query(default=False),
    include_obsolete: bool = Query(default=False),
    as_of: str | None = Query(default=None, max_length=20),
    canonical_first: bool = Query(default=False),
    background: BackgroundTasks = None,
) -> RecallV2Response:
    """Enhanced recall with HyDE, query expansion, reranking, time decay.

    Query params:
      hyde    = generate a hypothetical answer via Jenna and search with its embedding
      expand  = generate 3 query variants via Jenna, search each, RRF-merge
      rerank  = apply token-overlap reranker (default ON — cheap, always helps)
      decay   = apply exponential time decay per collection (default ON)
      since/until = temporal range (same as /recall)
      entity/collection/domain = filter passthrough
      source_type = filter personal collection results by type (note|message|event|reminder)
      canonical_first = Karpathy llm-wiki mode — query the canonical truth
          layer only (skips experience/obsidian/semantic_memory). Use when
          you want wiki-as-truth semantics. Fall back to a regular query
          without this flag if canonical is sparse.
    """
    if not q.strip():
        raise HTTPException(status_code=400, detail="q parameter required")

    # Response cache — identical queries within 30s return cached.
    # 2026-04-16 R-3: include session_id (from X-Session-Id header or
    # Authorization-derived fingerprint) in the cache key so spreading
    # activation + working-memory state doesn't leak between sessions.
    # Previously two concurrent sessions sharing a query got each other's
    # activation-boosted results.
    _sess_hdr = request.headers.get("x-session-id", "")
    _agent_hdr = request.headers.get("x-agent", "")
    # 2026-04-17 fix: include the active embedder's adapter path in the
    # cache key so adapter swaps (e.g. during A/B gate) don't serve stale
    # pre-adapter results. Without this, cached responses from the base
    # embedder get returned to adapter-path callers → zero measurable
    # delta in LoRA A/B even when the adapter genuinely changes rankings.
    try:
        from indexer import _lora_embedder as _active_adapter

        _adapter_marker = _active_adapter[0] if _active_adapter else "base"
    except Exception:
        _adapter_marker = "base"
    cache_key = (
        f"{q}:{n}:{hyde}:{expand}:{rerank}:{decay}:{iterative}:{collection}:"
        f"{domain}:{since}:{until}:{entity}:{source_type}:"
        f"{include_history}:{include_obsolete}:{as_of}:{canonical_first}:"
        f"sess={_sess_hdr}:agent={_agent_hdr}:emb={_adapter_marker}"
    )
    cached = _recall_cache_get(cache_key)
    if cached:
        return cached

    t_start = time.time()
    timing: dict[str, Any] = {}

    start_dt, end_dt = temporal.parse_range(since, until)
    # ChromaDB 1.4.1 rejects string operands in $gte/$lt; filter Python-side instead.
    where = None
    collections_arg = [collection] if collection else None
    # Widen inner-search n when a temporal filter will post-drop rows.
    search_n_mult = 3 if (start_dt or end_dt) else 2

    hypothetical: str | None = None
    variants: list[str] = [q]

    # Query expansion first — generates variants that downstream HyDE can also use.
    if expand:
        t_expand = time.time()
        try:
            variants = _hyde.expand_query(q, max_variants=3)
        except Exception:
            variants = [q]
        timing["expansion_ms"] = int((time.time() - t_expand) * 1000)

    # Run recall for each variant in parallel and RRF-fuse.
    t_search = time.time()
    all_payloads: list[dict] = []
    from concurrent.futures import ThreadPoolExecutor as _VariantPool
    from concurrent.futures import as_completed as _as_completed

    _sources = ["canonical"] if canonical_first else ["rag", "canonical", "obsidian"]

    def _run_variant(v_query):
        return search_unified.search_all(
            v_query,
            n * search_n_mult,
            sources=_sources,
            domain=domain,
            original_query=q,
            where=where,
            collections=collections_arg,
            entity=entity,
            explain=False,
            source_type=source_type,
            include_history=include_history,
            include_obsolete=include_obsolete,
            as_of=as_of,
        )

    if len(variants) == 1:
        try:
            all_payloads.append(_run_variant(variants[0]))
        except Exception:
            pass
    else:
        with _VariantPool(max_workers=min(len(variants), 4)) as _vpool:
            futures = {_vpool.submit(_run_variant, v): v for v in variants}
            for fut in _as_completed(futures):
                try:
                    all_payloads.append(fut.result())
                except Exception:
                    continue
    timing["search_ms"] = int((time.time() - t_search) * 1000)
    # Aggregate per-source timing from search_all payloads
    # Aggregate per-source timing. search_ms is wall-clock for the sequential variant
    # loop; individual source timings (rag_ms, canonical_ms, etc.) are per-call maxes
    # across variants since sources run in parallel within each search_all call.
    for p in all_payloads:
        for k, v in p.get("source_timing", {}).items():
            timing[k] = max(timing.get(k, 0), v)

    # Optionally replace query embedding via HyDE — it affects search_rag specifically.
    # We already ran the normal recall; if hyde=True we also run a second pass using
    # the hypothetical answer as the query text, which changes the vector embedding.
    if hyde:
        t_hyde = time.time()
        try:
            hypothetical = _hyde.generate_hypothetical(q)
            if hypothetical:
                hyde_payload = search_unified.search_all(
                    hypothetical,
                    n * search_n_mult,
                    sources=["rag", "canonical", "obsidian"],
                    domain=domain,
                    original_query=q,
                    where=where,
                    collections=collections_arg,
                    entity=entity,
                    explain=False,
                    source_type=source_type,
                    include_history=include_history,
                    include_obsolete=include_obsolete,
                    as_of=as_of,
                )
                all_payloads.append(hyde_payload)
        except Exception:
            pass
        timing["hyde_ms"] = int((time.time() - t_hyde) * 1000)

    # ChromaDB 1.4.1 can't range-filter string datetime fields, so apply the
    # temporal filter Python-side to each payload's results before RRF.
    if start_dt or end_dt:
        for p in all_payloads:
            if isinstance(p, dict) and p.get("results"):
                p["results"] = temporal.filter_by_created_at(p["results"], start_dt, end_dt)

    # Merge all result lists via RRF.
    result_lists = [p.get("results", []) for p in all_payloads if p.get("results")]
    if not result_lists:
        timing["total_ms"] = int((time.time() - t_start) * 1000)
        _metrics_buf.record_search_latency(timing["total_ms"], timing)
        return RecallV2Response(
            query=q,
            results=[],
            total_candidates=0,
            hyde_used=hyde,
            hypothetical=hypothetical,
            variants=variants if expand else [],
            rerank_applied=rerank,
            time_decay_applied=decay,
            latency_ms=int((time.time() - t_start) * 1000),
            timing=timing,
        )

    t_rrf = time.time()
    fused = _rrf.rrf_fuse(result_lists, id_key="path")
    timing["rrf_ms"] = int((time.time() - t_rrf) * 1000)

    # Two-stage rerank (2026-04-12):
    # 1. Token-overlap rerank.py — applies trust_boost (1.4x canonical), title
    #    overlap, source boost. Cheap, semantically naive but preserves the
    #    canonical-as-truth-layer principle.
    # 2. BGE-reranker-base cross-encoder — refines ordering with real semantic
    #    scoring. Blends with stage-1 output so trust boosts carry through.
    # When BRAIN_CROSS_ENCODER_ENABLED=false, only stage 1 runs.
    if rerank:
        t_rerank = time.time()
        # Stage 1 rerank is idempotent (2026-04-16 fix): search_all already
        # applied it per-variant and marked each result `_rerank_applied`.
        # Calling _rerank.rerank again is a no-op score-wise; it only
        # re-sorts. Previously the `len(variants) == 1` condition caused a
        # second multiplicative rerank pass for expand=True queries that
        # compounded trust/relevance boosts and flattened the top-K to the
        # [0,100] clamp ceiling.
        fused = _rerank.rerank(q, fused, top_k=None)
        for r in fused:
            r["score"] = r.get("rerank_score", r.get("score", 0))
        timing["rerank_ms"] = int((time.time() - t_rerank) * 1000)

        # Stage 2: real cross-encoder refinement on the top window
        ce_enabled = False
        try:
            from brain_core import config as _brain_config

            ce_enabled = bool(getattr(_brain_config, "BRAIN_CROSS_ENCODER_ENABLED", False))
        except Exception:
            ce_enabled = False

        if ce_enabled:
            t_ce = time.time()
            try:
                from brain_core.cross_encoder_rerank import rerank_with_cross_encoder

                # Only rerank the top window — tail stays ordered by stage 1.
                # cross_encoder_rerank overwrites `score` with a blend of the
                # stage-1 score (which already includes trust_boost) and CE signal.
                # top_k cut 20→14: for n≤10 responses the extra 6 rerank slots
                # almost never reshuffle the final top, and MPS batch time scales
                # linearly with pair count — ~30ms p95 saved on single queries and
                # a lot more under concurrent load where .predict() serializes.
                fused = rerank_with_cross_encoder(q, fused, top_k=14)
                timing["cross_encoder_ms"] = int((time.time() - t_ce) * 1000)
            except Exception as _ce_err:
                log.warning("cross-encoder rerank failed, stage-1 result stands: %s", _ce_err)

    # Apply time decay AFTER rerank so freshness actually affects the final ordering.
    # Decay multiplies into `score`, which is now either the raw RRF score (no rerank)
    # or the reranked score (with rerank).
    if decay:
        t_decay = time.time()
        fused = _time_decay.apply_to_results(fused)
        timing["decay_ms"] = int((time.time() - t_decay) * 1000)

    fused.sort(key=lambda r: r.get("score", 0), reverse=True)

    # Content enrichment pass: for file-backed top-N results, replace the
    # per-chunk content snippet with a longer excerpt read directly from the
    # source file. Retrieval ranking already happened; this just gives the
    # caller (and downstream UIs / eval tools) richer context for the same
    # document without disturbing rank order or latency-critical paths.
    t_enrich = time.time()
    _seen_paths: set[str] = set()
    _max_file_bytes = 4000  # cap per result so responses stay compact
    _enrichable_types = {
        "canonical-note",
        "distilled-note",
        "obsidian-note",
        "agent-config",
        "learning",
        "docker-compose",
        "nginx-conf",
    }
    for _r in fused[:n]:
        _path = _r.get("path", "")
        if not _path or _path in _seen_paths:
            continue
        _rtype = _r.get("type") or (_r.get("metadata") or {}).get("type") or ""
        if _rtype not in _enrichable_types:
            continue
        try:
            _p = Path(_path)
            if not _p.is_file():
                continue
            _txt = _p.read_text(errors="ignore")
        except Exception:
            continue
        # Prefer a window centered on the matched chunk's text to stay local
        # to what ranked, not a generic file head. Fall back to file head
        # if the chunk isn't found in the file anymore (stale chunks, edits).
        _chunk = _r.get("content") or ""
        _anchor = _chunk[:120] if _chunk else ""
        if _anchor and _anchor in _txt:
            _idx = _txt.index(_anchor)
            _start = max(0, _idx - 500)
            _end = min(len(_txt), _idx + _max_file_bytes - 500)
            _r["content"] = _txt[_start:_end]
        else:
            _r["content"] = _txt[:_max_file_bytes]
        _seen_paths.add(_path)
    timing["enrich_ms"] = int((time.time() - t_enrich) * 1000)

    # 2026-04-16 Tier 3 #14: metacognitive surface. Inject per-result
    # `confidence` (from atoms.confidence, Bayesian-updated ledger) and
    # `pending_contradictions` count (from semantic_contradictions) so
    # downstream callers can make informed decisions about trusting each
    # fact. The raw data has existed in brain.db + Chroma for weeks but
    # never flowed through to the recall response — a superhuman brain
    # should surface its own uncertainty, not hide it.
    t_meta = time.time()
    try:
        from atoms_store import _conn as _atoms_conn

        sm_ids = [
            r.get("id", "")
            for r in fused[:n]
            if isinstance(r, dict) and r.get("collection") == "semantic_memory" and r.get("id")
        ]
        if sm_ids:
            placeholders = ",".join("?" for _ in sm_ids)
            with _atoms_conn() as _c:
                rows = _c.execute(
                    f"SELECT chroma_id, confidence, trust_score "
                    f"FROM atoms WHERE chroma_id IN ({placeholders})",
                    sm_ids,
                ).fetchall()
            # 2026-04-16 Tier 3 #3: apply confidence calibration before
            # surfacing. If the weekly calibration job has fitted Platt
            # parameters, raw atom confidence is mapped through the
            # logistic transform; otherwise identity.
            try:
                from confidence_calibration import apply_calibration as _apply_cal
            except Exception:
                _apply_cal = lambda x: x  # type: ignore
            conf_by_id = {
                r["chroma_id"]: {
                    "confidence_raw": round(float(r["confidence"] or 0.5), 3),
                    "confidence": round(float(_apply_cal(float(r["confidence"] or 0.5))), 3),
                    "trust_score": round(float(r["trust_score"] or 0.5), 3),
                }
                for r in rows
            }
            for r in fused[:n]:
                if r.get("collection") != "semantic_memory":
                    continue
                row = conf_by_id.get(r.get("id", ""))
                if row:
                    r["confidence"] = row["confidence"]
                    r["confidence_raw"] = row["confidence_raw"]
                    r["trust_score_current"] = row["trust_score"]
    except Exception:
        pass

    # Pending-contradictions lookup — count unresolved semantic_contradictions
    # rows that reference any top result's chroma_id. This is the signal
    # that tells a caller "this fact has an open dispute."
    try:
        if fused:
            top_ids = [r.get("id", "") for r in fused[:n] if r.get("id")]
            if top_ids:
                points = get_vector_store().get(
                    "semantic_contradictions",
                    filter={
                        "$or": [
                            {"memory_id_a": {"$in": top_ids}},
                            {"memory_id_b": {"$in": top_ids}},
                        ]
                    },
                    limit=100,
                    with_payload=True,
                    with_documents=False,
                )
                contra_count: dict[str, int] = {}
                for p in points:
                    meta = p.payload or {}
                    if meta.get("resolved"):
                        continue
                    a, b = meta.get("memory_id_a"), meta.get("memory_id_b")
                    if a:
                        contra_count[a] = contra_count.get(a, 0) + 1
                    if b:
                        contra_count[b] = contra_count.get(b, 0) + 1
                for r in fused[:n]:
                    rid = r.get("id", "")
                    if rid and rid in contra_count:
                        r["pending_contradictions"] = contra_count[rid]
    except Exception:
        pass
    timing["metacognition_ms"] = int((time.time() - t_meta) * 1000)

    # 2026-04-16 Tier 3 #4 + R-10: retrieval-induced inhibition logging.
    # Record top as winner, rank 2–5 as losers on this query cue.
    # Dispatched to the search bg pool so we don't add SQLite write
    # latency to the hot recall path (~15ms saved on p95).
    try:
        if fused and len(fused) >= 2:
            _sm_results = [r for r in fused[:5] if r.get("collection") == "semantic_memory" and r.get("id")]
            if len(_sm_results) >= 2:
                from retrieval_inhibition import log_competition as _log_comp

                from brain_core.search_unified import _search_bg_pool as _bg

                _winner_id = _sm_results[0]["id"]
                _loser_ids = [r["id"] for r in _sm_results[1:]]
                _bg.submit(_log_comp, _winner_id, _loser_ids, q)
    except Exception:
        pass

    total_candidates = sum(p.get("total_candidates", 0) for p in all_payloads)
    timing["total_ms"] = int((time.time() - t_start) * 1000)
    timing["result_count"] = min(n, len(fused))
    timing["candidate_count"] = total_candidates

    # ── Phase M9: CRAG iterative retrieval (opt-in via ?iterative=true) ──
    # If the caller asked for iterative recall, score the result confidence
    # and trigger one query expansion + retry on low confidence. Capped at
    # 1 retry to bound latency. The retry recurses into recall_v2 with
    # iterative=False so it's a strict single-shot, no infinite loop.
    #
    # M8.4: Adaptive-RAG router can override the caller's iterative flag for
    # SIMPLE queries (where CRAG is pure latency cost with no recall benefit)
    # and for MULTI queries auto-enable CRAG even when the caller didn't ask.
    # Default OFF via BRAIN_ADAPTIVE_RAG env var. When disabled, the caller's
    # explicit `iterative=` param is honored as before.
    use_crag = iterative
    try:
        from brain_core.adaptive_rag import should_use_crag as _ar_should_use

        use_crag, _ar_reason = _ar_should_use(q, caller_explicit=iterative)
        timing["adaptive_rag"] = _ar_reason
    except Exception:
        use_crag = iterative

    if use_crag and fused:
        try:
            from brain_core.crag import (
                expand_query as _crag_expand_query,
            )
            from brain_core.crag import (
                score_confidence as _crag_score,
            )
            from brain_core.crag import (
                should_iterate as _crag_should_iterate,
            )

            t_crag = time.time()
            confidence_report = _crag_score(fused[: max(n, 5)])
            # 2026-04-16 Tier 3 #11: Self-RAG (Asai 2023) semantic critique
            # layer. When BRAIN_SELF_RAG_ENABLED=true, we dispatch Jenna to
            # score result relevance semantically and blend with the
            # heuristic. Replaces the token-shape-only confidence signal
            # with a real "does this answer the query?" judgment. Off by
            # default — costs ~1s Jenna call per iterative recall.
            try:
                from brain_core.self_rag import blend_with_heuristic as _blend_self_rag
                from brain_core.self_rag import critique as _self_rag_critique

                _sr = _self_rag_critique(q, fused[: max(n, 5)])
                if _sr.components.get("source") == "self_rag":
                    blended = _blend_self_rag(_sr.score, confidence_report.score)
                    confidence_report.score = blended
                    confidence_report.components = {
                        **confidence_report.components,
                        "self_rag_score": _sr.score,
                        "self_rag_components": _sr.components,
                        "blended": True,
                    }
            except Exception:
                pass
            crag_telemetry: dict[str, Any] = {
                "first_hop_confidence": confidence_report.score,
                "first_hop_components": confidence_report.components,
                "iterated": False,
            }
            if _crag_should_iterate(confidence_report):
                rewritten = _crag_expand_query(q, fused[:3])
                if rewritten and rewritten != q:
                    crag_telemetry["expanded_query"] = rewritten
                    # M7-WS7 C2 fix: recurse with iterative=False AND force
                    # hyde=False, expand=False to prevent the inner call from
                    # firing additional LLM dispatches. Worst case before this
                    # fix: 1 outer HyDE + 3 outer expand + 1 CRAG rewrite + 1
                    # inner HyDE + 1 inner expand = up to 7 LLM calls per req.
                    # After this fix: outer dispatches + 1 CRAG rewrite, max.
                    second_hop = recall_v2(
                        request,
                        q=rewritten,
                        n=n,
                        hyde=False,
                        expand=False,
                        rerank=rerank,
                        decay=decay,
                        iterative=False,
                        since=since,
                        until=until,
                        entity=entity,
                        collection=collection,
                        domain=domain,
                        source_type=source_type,
                        include_history=include_history,
                        include_obsolete=include_obsolete,
                        as_of=as_of,
                        background=background,
                    )
                    second_results = second_hop.results
                    second_report = _crag_score(second_results[: max(n, 5)])
                    crag_telemetry["second_hop_confidence"] = second_report.score
                    crag_telemetry["iterated"] = True
                    # Pick the higher-confidence result set
                    if second_report.score > confidence_report.score:
                        fused = second_results
                        crag_telemetry["selected"] = "second_hop"
                    else:
                        crag_telemetry["selected"] = "first_hop"
            timing["crag_ms"] = int((time.time() - t_crag) * 1000)
            timing["crag"] = crag_telemetry
        except Exception as _crag_err:
            log.warning("crag iterative path failed: %s", _crag_err)
            timing["crag_error"] = str(_crag_err)[:200]

    # M9.2: parent-child retrieval expand. When a child chunk wins the rank,
    # swap its content for the wider parent chunk so the LLM consumer gets
    # more context. Off by default; enabled via BRAIN_PARENT_CHILD_EXPAND.
    # Runs BEFORE community injection so parents are available for both
    # the child-expanded path and the community synthetic results.
    try:
        from brain_core.parent_child_expand import expand_to_parents as _pc_expand

        fused = _pc_expand(fused)
    except Exception as _pc_err:
        log.warning("parent-child expand failed: %s", _pc_err)

    # M8.7: inject GraphRAG community summaries for MULTI-class queries.
    # When adaptive_rag classifies a query as MULTI (comparison, reasoning,
    # multi-fact synthesis), the weekly-generated community summaries from
    # the entity graph Louvain clusters are prepended as a synthetic result
    # at rank 0 with a special source marker. Gives the caller cross-document
    # synthesis that single-doc retrieval can't provide.
    #
    # Cheap: the summaries are pre-computed and sit in a small table with
    # the entities indexed. get_summaries_matching does a single SELECT + a
    # substring check against the query terms (<5ms).
    #
    # Off when BRAIN_COMMUNITY_SUMMARIES is unset or when no community
    # matches the query entities.
    try:
        from brain_core.adaptive_rag import classify as _ar_classify
        from brain_core.community_summaries import get_summaries_matching as _cs_match

        _classification = _ar_classify(q)
        if _classification.label == "multi":
            _summaries = _cs_match(q, limit=2)
            if _summaries:
                # 2026-04-16 R-2 fix: score was hardcoded 95.0 which
                # always placed community summaries at rank 1 regardless
                # of whether they were actually the best answer,
                # overriding every Tier 1/2/3 scoring fix above. Now
                # scored relative to the current top result so they can
                # tiebreak or lead but not blindly dominate. Inserted
                # near top-K but not prepended — MMR + source diversity
                # still decide final placement.
                top_score = float(fused[0].get("score", 0.0)) if fused else 0.0
                # Community injected at 0.85×top: meaningful but not always rank-1.
                synth_score = max(55.0, min(100.0, top_score * 0.85)) if top_score > 0 else 70.0
                synthetic = []
                for s in _summaries:
                    synthetic.append(
                        {
                            "id": f"community:{','.join(s['entities'][:3])[:64]}",
                            "score": synth_score,
                            "source_type": "community",
                            "collection": "community_summaries",
                            "title": f"Community: {', '.join(s['entities'][:5])}",
                            "content": s["summary"],
                            "path": "graph/community/" + s.get("generated_at", ""),
                            "trust_tier": 2,  # derived, not canonical
                            "metadata": {
                                "entities": s["entities"],
                                "atom_count": s.get("atom_count", 0),
                                "generated_at": s.get("generated_at"),
                            },
                        }
                    )
                # Merge by score so they mix with real results rather than
                # always leading. MULTI queries still benefit because the
                # score is high enough to surface in top-3 typically.
                fused = sorted(fused + synthetic, key=lambda r: r.get("score", 0), reverse=True)
                timing["community_summaries_injected"] = len(synthetic)
    except Exception as _cs_err:
        log.warning("community summary inject failed: %s", _cs_err)

    _metrics_buf.record_search_latency(timing["total_ms"], timing)

    # 2026-04-17 Phase 4: proactive doubt meta-note.
    _meta_note = _build_meta_note(fused[:n])

    response = RecallV2Response(
        query=q,
        results=fused[:n],
        total_candidates=total_candidates,
        hyde_used=hyde and hypothetical is not None,
        hypothetical=hypothetical,
        variants=variants if expand else [],
        rerank_applied=rerank,
        time_decay_applied=decay,
        latency_ms=timing["total_ms"],
        timing=timing,
        meta_note=_meta_note,
    )
    _recall_cache_put(cache_key, response)

    # Gap logging: record queries where cross-encoder relevance is flat,
    # meaning the brain has nothing semantically close. The CE score is the
    # only signal that reflects real semantic match — blended `score` is
    # dominated by RRF ranks which always have a top-N winner even for
    # gibberish queries.
    #
    # Heuristic: log when max CE score < 0.52 (model is at the sigmoid midpoint,
    # indicating "I have no opinion"). Good queries see CE scores 0.55-0.75.
    # Only log unfiltered queries — filtered queries with no hits are usually
    # intentional.
    # Moved from /recall v1 on 2026-04-12; v1's max_score<5.0 threshold never fired.
    try:
        filter_free = not (
            collection
            or domain
            or entity
            or source_type
            or since
            or until
            or as_of
            or include_history
            or include_obsolete
        )
        if filter_free:
            results_list = fused[:n]
            ce_scores = [
                float(r.get("cross_encoder_score", 0))
                for r in results_list
                if r.get("cross_encoder_score") is not None
            ]
            max_ce = max(ce_scores, default=0.0)
            # Fall back to blended score threshold if CE wasn't run (flag off)
            max_score = max((float(r.get("score", 0)) for r in results_list), default=0.0)
            is_gap = (
                len(results_list) == 0
                or (ce_scores and max_ce < 0.52)
                or (not ce_scores and max_score < 30.0)
            )
            if is_gap:
                gap_log = BRAIN_DIR / "logs" / "recall-gaps.jsonl"
                gap_log.parent.mkdir(parents=True, exist_ok=True)
                with gap_log.open("a") as gf:
                    gf.write(
                        json.dumps(
                            {
                                "timestamp": datetime.now(UTC).isoformat(),
                                "query": q[:500],
                                "n_results": len(results_list),
                                "max_score": round(max_score, 2),
                                "max_ce_score": round(max_ce, 4) if ce_scores else None,
                                "endpoint": "/recall/v2",
                            }
                        )
                        + "\n"
                    )
    except Exception:
        pass

    # Auto-record search feedback + adoption tracking — both fire-and-forget.
    # M7-WS7 H3 fix: insert_action_audit was previously synchronous on the
    # response path (0.5-30ms per call under writer contention). Both the
    # auto-feedback recorder and the adoption tracker now share the same
    # background dispatch so neither blocks the response.
    agent = request.headers.get("x-agent") or request.query_params.get("actor") or "unknown"

    def _post_recall_side_effects() -> None:
        _record_auto_feedback(q, fused[:n], agent)
        try:
            from brain_core.atoms_store import insert_action_audit as _iaa

            # Normalize ids to dashed UUID form so downstream readers
            # (recall_judge, contradiction propagation, audit dashboards) can
            # round-trip them back to Qdrant points. The recall result builder
            # was emitting hex32 (UUID with dashes stripped); writing those
            # raw left the audit rows opaque and unmappable.
            def _to_dashed_uuid(raw: str) -> str:
                if not raw:
                    return raw
                if len(raw) == 32 and "-" not in raw and all(c in "0123456789abcdef" for c in raw.lower()):
                    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"
                return raw

            _iaa(
                route="/recall/v2",
                tool="brain_recall",
                actor=agent,
                query_text=q[:500],
                retrieved_chroma_ids=[
                    _to_dashed_uuid(str(r.get("id") or r.get("chroma_id") or ""))[:64]
                    for r in fused[:n]
                    if r.get("id") or r.get("chroma_id")
                ][:20],
            )
        except Exception:
            pass

    if background is not None:
        background.add_task(_post_recall_side_effects)
    else:
        try:
            from brain_core.search_unified import _search_bg_pool

            _search_bg_pool.submit(_post_recall_side_effects)
        except Exception:
            pass

    return response


# 2026-04-16 Tier 3 #13: SSE streaming recall — push-based context.
# Clients (brain-ui, agent hooks) can open a persistent connection and
# receive ranked result chunks as each source in search_unified returns,
# rather than waiting for the full RRF+rerank pipeline. Enables
# mid-conversation context injection (proactive brain). The stream emits
# partial source payloads in arrival order, then a final fused top-K,
# then closes.
@app.get("/recall/stream", tags=["recall"], dependencies=[Depends(verify_bearer)])
def recall_stream(
    q: str,
    n: int = Query(default=10, ge=1, le=50),
    agent: str = "unknown",
) -> StreamingResponse:
    """Server-Sent Events stream of recall results.

    Events emitted (all as `event: <name>\\ndata: <json>\\n\\n`):
      - `source` — one per completed source (rag, canonical, obsidian,
        graph, fts, graph_prefetch) with that source's top-k chunk
      - `fused` — final RRF-fused + reranked top-n after all sources
      - `end` — terminator
    """
    if not q or not q.strip():
        raise HTTPException(status_code=400, detail="q required")

    def _gen():
        import queue as _queue

        q_out: _queue.Queue = _queue.Queue()
        rid = get_request_id() or ""
        t_start = time.time()

        def _run_source(name: str, fn):
            try:
                result = fn()
                q_out.put(
                    (
                        "source",
                        {"name": name, "results": result[:n] if isinstance(result, list) else [], "rid": rid},
                    )
                )
            except Exception as e:
                q_out.put(("source", {"name": name, "error": str(e)[:200], "rid": rid}))

        # Dispatch the same sources search_unified knows about in parallel
        # threads. When each returns, push a "source" event; downstream
        # consumers can start using partial results immediately while the
        # rest are still in flight.
        try:
            import threading as _t

            from brain_core.search_unified import search_all as _search_all

            def _full_search():
                try:
                    payload = _search_all(q, limit=n)
                    q_out.put(
                        (
                            "fused",
                            {
                                "results": payload.get("results", [])[:n],
                                "source_timing": payload.get("source_timing", {}),
                                "rid": rid,
                                "latency_ms": int((time.time() - t_start) * 1000),
                            },
                        )
                    )
                except Exception as e:
                    q_out.put(("fused", {"error": str(e)[:200], "rid": rid}))
                finally:
                    q_out.put(("end", {"rid": rid}))

            _t.Thread(target=_full_search, daemon=True).start()
        except Exception as e:
            q_out.put(("end", {"error": str(e)[:200], "rid": rid}))

        # Pump events to the client. Cap wall-clock at 20s so a hung
        # source cannot indefinitely hold the SSE connection open.
        deadline = time.time() + 20.0
        while True:
            timeout = max(0.05, deadline - time.time())
            try:
                kind, payload = q_out.get(timeout=timeout)
            except _queue.Empty:
                # Heartbeat for intermediaries
                yield b": keepalive\n\n"
                if time.time() >= deadline:
                    yield b'event: end\ndata: {"reason": "timeout"}\n\n'
                    break
                continue
            line = f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            yield line.encode("utf-8")
            if kind == "end":
                break

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",  # disable nginx buffering
    }
    return StreamingResponse(_gen(), media_type="text/event-stream", headers=headers)


# 2026-04-17 H-3: agent-ergonomic batch endpoints. AI agents (Claude
# Code, OpenClaw agents) often fan out N recalls per task. Serial
# round-trips add up fast — a single batch endpoint lets the agent
# submit a list of queries and get a list of results back in one
# HTTP call. 20-query cap per batch to keep per-call latency bounded.
class RecallBatchRequest(BaseModel):
    queries: list[str] = Field(..., max_length=20, min_length=1)
    n: int = Field(default=5, ge=1, le=20)
    rerank: bool = True
    decay: bool = True
    agent: str = Field(default="unknown", max_length=64)


@app.post("/recall/batch", tags=["recall"], dependencies=[Depends(verify_bearer)])
@limiter.limit("300/minute")
def recall_batch(request: Request, req: RecallBatchRequest) -> dict:
    """Batch recall — submit up to 20 queries in one HTTP call.

    Returns `{"results": [{"query": q, "hits": [...]}, ...]}`. Each
    query runs through the full /recall/v2 pipeline (rerank, decay,
    canonical trust override, metacognition enrichment). Queries run
    in parallel via the shared variant pool to minimize latency.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    import search_unified as _su

    out: list[dict] = []

    def _run_one(q: str) -> dict:
        try:
            payload = _su.search_all(q, limit=req.n)
            return {"query": q, "hits": (payload.get("results") or [])[: req.n]}
        except Exception as e:
            return {"query": q, "error": str(e)[:200]}

    with ThreadPoolExecutor(max_workers=min(len(req.queries), 8)) as pool:
        futures = {pool.submit(_run_one, q): q for q in req.queries}
        for fut in as_completed(futures):
            try:
                out.append(fut.result())
            except Exception as e:
                out.append({"query": futures[fut], "error": str(e)[:200]})
    return {"results": out, "count": len(out)}


# /agent/heartbeat moved to brain_core/routes/insights.py


@app.post("/recall/feedback", tags=["recall"], dependencies=[Depends(verify_bearer)])
def search_feedback(req: SearchFeedbackRequest):
    """Record user feedback on search results. Reinforces memory via MemRL."""
    try:
        feedback_log = BRAIN_DIR / "logs" / "search-feedback.jsonl"
        feedback_log.parent.mkdir(parents=True, exist_ok=True)
        with feedback_log.open("a") as f:
            f.write(
                json.dumps(
                    {
                        "timestamp": datetime.now(UTC).isoformat(),
                        "query": req.query,
                        "result_id": req.result_id,
                        "source": req.result_source,
                        "useful": req.useful,
                        "agent": req.agent,
                    }
                )
                + "\n"
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("feedback log write", e))

    # Reinforce memory if it's a semantic_memory result.
    # 2026-04-16 fix: result_id is a raw Chroma UUID, not prefixed with
    # "semantic_memory:" — that check never matched and the reinforcement
    # was dead code. Dispatch based on result_source (the collection name
    # that recall_v2 actually populates at server.py:1489).
    if req.result_id and req.result_source == "semantic_memory":
        try:
            from entity_graph import reinforce_memory

            reinforce_memory(req.result_id, success=req.useful)
        except Exception:
            pass

    # Phase 7: eval auto-growth signal
    proposal_id: str | None = None
    if req.wrong_answer and req.expected:
        try:
            from eval_proposals import insert_proposal

            proposal_id = insert_proposal(
                query=req.query,
                expected=req.expected,
                source_event="recall_feedback",
                confidence=0.7,
            )
        except Exception:
            pass

    return {"status": "recorded", "eval_proposal_id": proposal_id}


# ── Routes: /brain/ingest/image ── moved to brain_core/routes/ingest.py


# ── Routes: /brain/wm/* ── moved to brain_core/routes/wm.py


# ── Routes: /recall/active — per-turn thalamus (v3 plan) ─────────────────
@app.post(
    "/recall/active",
    response_model=RecallActiveResponse,
    tags=["recall"],
    dependencies=[Depends(verify_bearer)],
)
@limiter.limit("3000/minute")
def recall_active(request: Request, req: RecallActiveRequest) -> dict:
    """Per-turn attention gating. Called from claude_boot.sh (UserPromptSubmit)
    and OpenClaw before_prompt_build plugin on EVERY user turn.

    Returns intent-routed canonical guarantees + semantic hits + proactive
    alerts + doorbell messages, dedup'd against session_context['recall_seen'].

    Fail-open: any internal failure returns degraded=True with empty blocks
    rather than a 500. Hook scripts must never block the user's prompt.
    """
    # 2026-04-17 hook adoption metrics — count per-agent calls so we can see
    # whether OpenClaw's brain-active-recall hook is actually firing across
    # all 5 agents, not just Claude Code. Surfaces in /metrics under
    # hook_adoption. No persistence — in-memory counter, resets on restart.
    # Log-on-first-failure so a structural bug in metrics_buffer surfaces
    # instead of silently losing all hook telemetry.
    global _hook_metrics_warned
    try:
        _metrics_buf.record_hook_call("recall_active", req.agent or "unknown")
    except Exception:
        if not _hook_metrics_warned:
            log.warning("hook metrics recording failed (suppressing further)", exc_info=True)
            _hook_metrics_warned = True
    t0 = time.time()
    result = active_recall.build_injection(
        prompt=req.prompt,
        session_id=req.session_id,
        turn_idx=req.turn_idx,
        agent=req.agent,
        cwd=req.cwd,
        seen_hashes=req.seen_hashes,
    )
    try:
        _metrics_buf.record_hook_latency("recall_active", int((time.time() - t0) * 1000))
    except Exception:
        if not _hook_metrics_warned:
            log.warning("hook latency recording failed (suppressing further)", exc_info=True)
            _hook_metrics_warned = True
    return result


# ── Routes: /brain/reason/multihop ── moved to brain_core/routes/reasoning.py


# ── Routes: /chris/think — decision endpoint in Chris's first-person voice ──
# CHRIS_THINK_PROMPT + /chris/think moved to brain_core/routes/think.py


# boot-context routes moved to brain_core/routes/reasoning.py

# ── Routes: synthesis read ── moved to brain_core/routes/synthesis.py


# ── Routes: capture (POST) ── moved to brain_core/routes/capture.py


# ── Routes: /brain/speak, /brain/command ── moved to brain_core/routes/speak.py, command.py


# ── Routes: /brain/canonical_staleness, /brain/self_eval ── moved to brain_core/routes/admin_ops.py


# ── Routes: coding_events ── moved to brain_core/routes/coding.py


# ── Jobs: shared dispatcher (used by POST /jobs/{name} and the scheduler) ──
# _dispatch_job + _wait_for_job moved to brain_core/job_registry.py


# ── Routes: jobs ── moved to brain_core/routes/jobs.py


# (scheduler lifespan is wired above where `app` is created)


# ── Routes: self-learning ── moved to brain_core/routes/learn.py


# ── Routes: memory CRUD ─────────────────────────────────
# 2026-04-21: helpers now return collection NAMES under the VectorStore
# abstraction (Phase A2 of Qdrant migration). Callers previously received
# a ChromaDB UUID and interpolated it into raw REST URLs; with VectorStore
# we address by name everywhere. Kept the same function names so the call
# sites don't have to change signature.
def _memory_collection_id() -> str:
    get_vector_store().create_collection(learn.SEMANTIC_COLLECTION)
    return learn.SEMANTIC_COLLECTION


def _contradictions_collection_id() -> str:
    get_vector_store().create_collection(learn.CONTRADICTIONS_COLLECTION)
    return learn.CONTRADICTIONS_COLLECTION


# ── /memory GET response cache (30s TTL) ──
_memory_list_cache: dict[str, tuple[float, "MemoryListResponse"]] = {}
_memory_list_lock = threading.Lock()
# In-flight map: key → Event. Second caller with the same key waits for the
# first to finish and then re-reads the cache, instead of issuing a duplicate
# 300ms Chroma fetch. Prevents cache stampede on cold UI polls.
_memory_list_inflight: dict[str, threading.Event] = {}
_MEMORY_LIST_TTL = 30.0
_MEMORY_LIST_MAX = 100


def _memory_cache_key(limit: int, offset: int, category: str | None, agent: str | None) -> str:
    return f"{limit}:{offset}:{category or ''}:{agent or ''}"


@app.get("/memory", response_model=MemoryListResponse, tags=["memory"], dependencies=[Depends(verify_bearer)])
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
            MemoryEntry(id=p.id, content=p.document or "", metadata=p.payload or {})
            for p in points
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


@app.get(
    "/memory/contradictions",
    response_model=ContradictionListResponse,
    tags=["memory"],
    dependencies=[Depends(verify_bearer)],
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

    entries: list[ContradictionEntry] = []
    for p in points:
        i = p.id
        doc = p.document or ""
        meta = p.payload or {}
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
            )
        )
    return ContradictionListResponse(results=entries, total=total)


@app.get("/memory/export", tags=["memory"], dependencies=[Depends(verify_bearer)])
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


@app.get(
    "/memory/{mem_id}", response_model=MemoryEntry, tags=["memory"], dependencies=[Depends(verify_bearer)]
)
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


@app.post("/memory", response_model=MemoryEntry, tags=["memory"], dependencies=[Depends(verify_bearer)])
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
        if should_delete_by_content(req.content):
            operation = "DELETE"
        else:
            operation = op
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
        try:
            store.delete(collection, ids=[supersede_target])
        except Exception as e:
            print(f"WARNING DELETE failed to remove {supersede_target}: {e}")
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
        try:
            store.update_payload(
                collection,
                ids=[supersede_target],
                patch={"superseded_by": mem_id, "valid_until": now_iso},
            )
        except Exception as e:
            print(f"WARNING failed to mark {supersede_target} superseded: {e}")

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
    # Fire hook (Phase 6A)
    try:
        import hooks

        hooks.fire("on_memory_stored", mem_id=mem_id, category=req.category, operation=operation)
    except Exception:
        pass

    # CR7 fix (2026-04-14): atoms mirror + v3 Brain Hygiene pipeline is now
    # a shared helper (ingest_mirror.mirror_memory) so /memory/batch, /learn,
    # and wm_consolidate can reuse the exact same block. Previously only
    # POST /memory went through the hygiene pipeline — batch was an
    # implicit bypass. HR4 fix: log errors instead of bare-except swallow.
    try:
        from atoms_store import mark_superseded
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


@app.post("/memory/batch", tags=["memory"], dependencies=[Depends(verify_bearer)])
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
            if should_delete_by_content(mem_req.content):
                operation = "DELETE"
            else:
                operation = op
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
        except Exception as e:
            print(f"WARNING batch supersede failed: {e}")

    # Apply deletes (batched)
    if deletes_to_apply:
        try:
            store.delete(col_id, ids=deletes_to_apply)
        except Exception as e:
            print(f"WARNING batch delete failed: {e}")

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
        for r in results:
            rid = r.get("id")
            if rid in batch_contradictions:
                r["contradictions"] = batch_contradictions[rid]

    return {
        "stored": len(ids_to_upsert),
        "superseded": len(supersede_updates),
        "deleted": len(deletes_to_apply),
        "total_requested": len(req.memories),
        "contradictions_found": sum(len(v) for v in batch_contradictions.values()),
        "results": results,
    }


@app.patch(
    "/memory/{mem_id}", response_model=MemoryEntry, tags=["memory"], dependencies=[Depends(verify_bearer)]
)
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


@app.get("/brain/doubt", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
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
      }
    """
    import sqlite3 as _sql

    out: dict = {"low_confidence_atoms": [], "pending_contradictions": [], "stale_canonical": []}

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

    return out


@app.post("/brain/consolidate", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
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


@app.delete("/memory/{mem_id}", tags=["memory"], dependencies=[Depends(verify_bearer)])
def delete_memory(mem_id: Annotated[str, PathParam()]) -> dict:
    collection = _memory_collection_id()
    try:
        get_vector_store().delete(collection, ids=[mem_id])
    except Exception as e:
        raise HTTPException(status_code=502, detail=_safe_http_detail("vector delete", e))
    return {"status": "deleted", "id": mem_id}


@app.post(
    "/memory/contradictions/{contra_id}/resolve", tags=["memory"], dependencies=[Depends(verify_bearer)]
)
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


@contextmanager
def _votes_conn():
    import sqlite3

    db = BRAIN_DIR / "logs" / "autonomy.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS contradiction_votes (
                contradiction_id TEXT NOT NULL,
                voter_agent TEXT NOT NULL,
                vote TEXT NOT NULL,
                confidence REAL NOT NULL,
                reasoning TEXT,
                voted_at TEXT NOT NULL,
                PRIMARY KEY (contradiction_id, voter_agent)
            )
        """)
        yield conn
    finally:
        conn.close()


@app.post("/memory/contradictions/{contra_id}/vote", tags=["memory"], dependencies=[Depends(verify_bearer)])
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


@app.get(
    "/brain/contradictions",
    response_model=ContradictionListResponse,
    tags=["memory"],
    dependencies=[Depends(verify_bearer)],
)
def list_contradictions_brain_alias(limit: int = 50) -> ContradictionListResponse:
    """Alias of GET /memory/contradictions for consistent /brain/* namespacing."""
    return list_contradictions(limit=limit)


@app.post("/brain/contradictions/{contra_id}/resolve", tags=["memory"], dependencies=[Depends(verify_bearer)])
def resolve_contradiction_brain_alias(
    contra_id: Annotated[str, PathParam()],
    req: ContradictionResolveRequest,
) -> dict:
    """Alias of POST /memory/contradictions/{id}/resolve."""
    return resolve_contradiction(contra_id=contra_id, req=req)


@app.post("/brain/contradictions/{contra_id}/vote", tags=["memory"], dependencies=[Depends(verify_bearer)])
def vote_on_contradiction_brain_alias(
    contra_id: Annotated[str, PathParam()], req: ContradictionVoteRequest
) -> dict:
    """Alias of POST /memory/contradictions/{id}/vote."""
    return vote_on_contradiction(contra_id=contra_id, req=req)


@app.get("/brain/contradictions/{contra_id}/votes", tags=["memory"], dependencies=[Depends(verify_bearer)])
def get_contradiction_votes_brain_alias(contra_id: Annotated[str, PathParam()]) -> dict:
    """Alias of GET /memory/contradictions/{id}/votes."""
    return get_contradiction_votes(contra_id=contra_id)


@app.get("/memory/contradictions/{contra_id}/votes", tags=["memory"], dependencies=[Depends(verify_bearer)])
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


@app.get("/brain/timetravel", tags=["brain"], dependencies=[Depends(verify_bearer)])
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
            "total_all_time": len(ids),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e))


# /brain/changes + /brain/evolution moved to brain_core/routes/governance.py


# ── Phase E1: Session context API ──
# _session_conn + E1/E2/E4/F1/code/tools/accuracy/outcomes/procedures moved to brain_core/routes/ops.py


# ── observability + schema + self-heal + admin ── moved to brain_core/routes/health.py


# ── Mount extracted route modules ───────────────────────
from routes.admin_ops import router as _admin_ops_router  # noqa: E402
from routes.agency import router as _agency_router  # noqa: E402
from routes.brain_ops import router as _brain_ops_router  # noqa: E402
from routes.capture import router as _capture_router  # noqa: E402
from routes.coding import router as _coding_router  # noqa: E402
from routes.decide import router as _decide_router  # noqa: E402
from routes.governance import router as _governance_router  # noqa: E402
from routes.health import router as _health_router  # noqa: E402
from routes.command import router as _command_router  # noqa: E402
from routes.ingest import router as _ingest_router  # noqa: E402
from routes.insights import router as _insights_router  # noqa: E402
from routes.jobs import router as _jobs_router  # noqa: E402
from routes.knowledge import router as _knowledge_router  # noqa: E402
from routes.learn import router as _learn_router  # noqa: E402
from routes.metrics import router as _metrics_router  # noqa: E402
from routes.liveness import router as _liveness_router  # noqa: E402
from routes.ops import router as _ops_router  # noqa: E402
from routes.profile import router as _profile_router  # noqa: E402
from routes.reasoning import router as _reasoning_router  # noqa: E402
from routes.speak import router as _speak_router  # noqa: E402
from routes.stores import router as _stores_router  # noqa: E402
from routes.synthesis import router as _synthesis_router  # noqa: E402
from routes.think import router as _think_router  # noqa: E402
from routes.web import router as _web_router  # noqa: E402
from routes.wm import router as _wm_router  # noqa: E402

app.include_router(_liveness_router)
app.include_router(_admin_ops_router)
app.include_router(_profile_router)
app.include_router(_web_router)
app.include_router(_brain_ops_router)
app.include_router(_stores_router)
app.include_router(_reasoning_router)
app.include_router(_synthesis_router)
app.include_router(_coding_router)
app.include_router(_learn_router)
app.include_router(_ingest_router)
app.include_router(_wm_router)
app.include_router(_capture_router)
app.include_router(_knowledge_router)
app.include_router(_governance_router)
app.include_router(_ops_router)
app.include_router(_health_router)
app.include_router(_metrics_router)
app.include_router(_insights_router)
app.include_router(_decide_router)
app.include_router(_jobs_router)
app.include_router(_think_router)
app.include_router(_agency_router)
app.include_router(_speak_router)
app.include_router(_command_router)


# ── Bootstrap ───────────────────────────────────────────
def main() -> None:
    secret = _load_secret()
    if not secret:
        sys.stderr.write(
            f"FATAL: no secret found at {SECRET_FILE}. "
            f"Generate: openssl rand -hex 32 > {SECRET_FILE} && chmod 600 {SECRET_FILE}\n"
        )
        sys.exit(2)

    import uvicorn

    sys.stderr.write(
        f"brain-server (FastAPI) v2.0 listening on http://{LISTEN_HOST}:{LISTEN_PORT}\n"
        f"  in-process search: rag={search_unified._RAG_IN_PROCESS} canonical={search_unified._CANONICAL_IN_PROCESS}\n"
        f"  jobs registered: {len(JOB_REGISTRY)}\n"
        f"  OpenAPI docs at: http://{LISTEN_HOST}:{LISTEN_PORT}/docs\n"
    )
    uvicorn.run(
        app,
        host=LISTEN_HOST,
        port=LISTEN_PORT,
        log_level="info",
        access_log=True,
    )


if __name__ == "__main__":
    main()
