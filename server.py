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

# launchd strips /usr/sbin from PATH, which makes joblib/loky probe physical
# CPU cores via a failing sysctl path and can leave noisy resource-tracker
# state on shutdown. Set these before any model/sklearn imports.
os.environ.setdefault("LOKY_MAX_CPU_COUNT", "8")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

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

# _hook_metrics_warned moved to brain_core/routes/recall.py (its only user)
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
    _request_id_ctx,
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


# JobResponse moved to brain_core/routes/jobs.py
# RecallResult* + RecallActive* + InjectionBlockModel moved to brain_core/routes/recall.py



# ImageIngestRequest moved to brain_core/routes/ingest.py

# WorkingMemorySetRequest/Item moved to brain_core/routes/wm.py



# Think* models moved to brain_core/routes/think.py


# ── Decision / reasoning models ─────────────────────────
# Decide/Reason models moved to brain_core/routes/decide.py


# ── Autonomy models ────────────────────────────────────
# Autonomy pydantic models moved to brain_core/routes/agency.py


# ── Self-learning + memory CRUD models ─────────────────
# LearnRequest / LearnResponse moved to brain_core/routes/learn.py









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
    for noisy_logger in ("httpx", "httpcore", "qdrant_client"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)
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
    # Warm the real cross-encoder only when it is intentionally in-process.
    # In worker mode, importing cross_encoder_model here defeats the RSS
    # isolation and pins Torch/MPS memory inside the long-running API process.
    try:
        from brain_core import config as _brain_config

        if getattr(_brain_config, "BRAIN_CROSS_ENCODER_ENABLED", False) and os.getenv(
            "BRAIN_RERANKER_MODE", "inprocess"
        ).strip().lower() != "worker":
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

# _request_id_ctx + get_request_id moved to brain_core/api_deps.py


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


# ── Routes: recall suite ── moved to brain_core/routes/recall.py


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


# ── Routes: memory CRUD + contradictions + /brain/timetravel ── moved to brain_core/routes/memory.py


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
from routes.memory import router as _memory_router  # noqa: E402
from routes.metrics import router as _metrics_router  # noqa: E402
from routes.liveness import router as _liveness_router  # noqa: E402
from routes.dashboard import router as _dashboard_router  # noqa: E402
from routes.session import router as _session_router  # noqa: E402
from routes.recall import router as _recall_router  # noqa: E402
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
app.include_router(_session_router)
app.include_router(_dashboard_router)
app.include_router(_health_router)
app.include_router(_metrics_router)
app.include_router(_insights_router)
app.include_router(_decide_router)
app.include_router(_jobs_router)
app.include_router(_memory_router)
app.include_router(_recall_router)
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
