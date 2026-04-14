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

import hmac
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

import structlog
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Path as PathParam, Query, Request

log = structlog.get_logger("brain.server")
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

# In-process modules — brain_core is the single source of truth.
_BRAIN_CORE = str(Path(__file__).parent / "brain_core")
sys.path.insert(0, _BRAIN_CORE)
import search_unified  # noqa: E402
import temporal  # noqa: E402
import boot_context  # noqa: E402
import learn  # noqa: E402
import hyde as _hyde  # noqa: E402
import rerank as _rerank  # noqa: E402
import rrf as _rrf  # noqa: E402
import time_decay as _time_decay  # noqa: E402
from openclaw_dispatch import dispatch as _openclaw_dispatch  # noqa: E402
from metrics_buffer import metrics_buffer as _metrics_buf  # noqa: E402
from indexer import (  # noqa: E402
    chroma_api as _chroma_api,
    get_embedding as _get_embedding,
    ensure_collection as _ensure_collection,
    _get_collection_id as _get_col_id,
)
from scheduler import brain_scheduler  # noqa: E402

# ── Config ──────────────────────────────────────────────
from config import (  # noqa: E402
    SECRET_FILE, INBOX_DIR, IDENTITY_FILE, STATE_FILE, DISTILLED_DAILY,
    WEEKLY_DIR, MONTHLY_DIR, FAILURE_LOG, BRAIN_DIR, PYTHON,
)
LISTEN_HOST = os.getenv("BRAIN_SERVER_HOST", "127.0.0.1")
LISTEN_PORT = int(os.getenv("BRAIN_SERVER_PORT", "8791"))

PROFILE_CACHE_TTL = 60

_py = PYTHON
_bd = str(BRAIN_DIR)
JOB_REGISTRY: dict[str, list[str]] = {
    # Ingestion
    "personal_ingest":    ["/bin/bash", f"{_bd}/ingest/run_personal.sh"],
    "gmail_ingest":       [_py, f"{_bd}/ingest/gmail.py"],
    "browser_ingest":     [_py, f"{_bd}/ingest/browser.py"],
    "shell_ingest":       [_py, f"{_bd}/ingest/shell_history.py"],
    "obsidian_sync":      [_py, f"{_bd}/ingest/obsidian.py", "pull"],
    "healthcheck":        [_py, f"{_bd}/ingest/healthcheck.py"],
    "ghost_blog_ingest":  [_py, f"{_bd}/ingest/ghost_blog.py"],
    # New data source ingest (agent-distilled)
    "openclaw_sessions_ingest":  [_py, f"{_bd}/ingest/openclaw_sessions.py"],
    "openclaw_sessions_ingest_market": [_py, f"{_bd}/ingest/openclaw_sessions.py", "--agents", "market", "--max-sessions", "20"],
    "openclaw_sessions_ingest_jenna":  [_py, f"{_bd}/ingest/openclaw_sessions.py", "--agents", "jenna",  "--max-sessions", "20"],
    "openclaw_sessions_ingest_liz":    [_py, f"{_bd}/ingest/openclaw_sessions.py", "--agents", "liz",    "--max-sessions", "20"],
    "openclaw_sessions_ingest_ellie":  [_py, f"{_bd}/ingest/openclaw_sessions.py", "--agents", "ellie",  "--max-sessions", "20"],
    "openclaw_sessions_ingest_sage":   [_py, f"{_bd}/ingest/openclaw_sessions.py", "--agents", "sage",   "--max-sessions", "20"],
    "claude_code_sessions_ingest": [_py, f"{_bd}/ingest/claude_code_sessions.py"],
    "git_activity_ingest":       [_py, f"{_bd}/ingest/git_activity.py"],
    "screen_time_ingest":        [_py, f"{_bd}/ingest/screen_time.py"],
    "active_contacts_ingest":    [_py, f"{_bd}/ingest/active_contacts.py"],
    # Synthesis
    "daily_synthesis":    [_py, f"{_bd}/synthesis/daily.py"],
    "daily_reflection":   [_py, f"{_bd}/synthesis/reflection.py"],
    "weekly_synthesis":   [_py, f"{_bd}/synthesis/weekly.py"],
    "monthly_synthesis":  [_py, f"{_bd}/synthesis/monthly.py"],
    "brain_reflect":      [_py, f"{_bd}/synthesis/reflect.py"],
    "profile_regen":      [_py, f"{_bd}/synthesis/profile_regen.py"],
    "proactive_check":    [_py, f"{_bd}/brain_core/proactive.py"],
    # Maintenance
    "memory_lifecycle":   [_py, f"{_bd}/brain_core/memory_lifecycle.py"],
    "canonical_pipeline": [_py, f"{_bd}/pipeline/pipeline_auto.py"],
    # Two-track eval (incident 2026-04-13): stable=strict gate+heal, extended=trend only.
    # `eval_run` aliases to the stable track so legacy scheduled triggers keep working.
    "eval_run":           [_py, f"{_bd}/cli/eval_gate.py", "--eval-set", f"{_bd}/cli/eval_set_stable.json", "--baseline", f"{_bd}/cli/eval_baseline_stable.json", "--track", "stable"],
    "eval_run_stable":    [_py, f"{_bd}/cli/eval_gate.py", "--eval-set", f"{_bd}/cli/eval_set_stable.json", "--baseline", f"{_bd}/cli/eval_baseline_stable.json", "--track", "stable"],
    "eval_run_extended":  [_py, f"{_bd}/cli/eval_gate.py", "--eval-set", f"{_bd}/cli/eval_set_extended.json", "--baseline", f"{_bd}/cli/eval_baseline_extended.json", "--track", "extended", "--no-heal", "--threshold", "10"],
    "eval_run_full":      [_py, f"{_bd}/cli/eval_gate.py", "--track", "full", "--no-heal", "--threshold", "10"],
    # Phase 4: SM-2 nightly review scheduler — seeds null next_review_at + obsoletes stale atoms
    "sm2_nightly":        [_py, "-c", f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from sm2 import nightly_pass; import json; print(json.dumps(nightly_pass()))"],
    # Phase 7: closed-loop self-learning jobs
    "autonomy_proposer":  [_py, "-c", f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from autonomy_proposer import run; import json; print(json.dumps(run()))"],
    "lora_ab_gate":       [_py, f"{_bd}/cli/lora_ab_gate.py"],
    # Phase C: eval auto-growth pipeline (run after lora_ab_gate but before sm2_nightly)
    "eval_holdout_promote": [_py, "-c", f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from eval_holdout_promote import run; import json; print(json.dumps(run()))"],
    "eval_holdout_audit":   [_py, "-c", f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from eval_holdout_audit import run; import json; print(json.dumps(run()))"],
    # Phase E: SLO check job — runs every 5 min, dispatches Telegram alerts on breach
    "slos_check":           [_py, "-c", f"import sys; sys.path.insert(0, '{_bd}/brain_core'); from slos import run; import json; print(json.dumps(run()))"],
    # Phase J2: adaptive HNSW ef_search tuning (advisory — applied on next collection load)
    "hnsw_tune":          [_py, "-c", f"import sys; sys.path.insert(0, '{_bd}/brain_core/pipeline'); sys.path.insert(0, '{_bd}/brain_core'); from hnsw_tuner import adaptive_tune; import json; print(json.dumps(adaptive_tune(dry_run=False)))"],
    "reindex":            ["/bin/zsh", f"{_bd}/cli/reindex.sh"],
    # Maintenance
    "log_rotation":       [_py, f"{_bd}/brain_core/maintenance.py", "all_cleanup"],
    "chroma_integrity":   [_py, f"{_bd}/brain_core/maintenance.py", "chroma_integrity"],
    "canonical_index":    ["/bin/bash", "-c", "SECRET=$(cat ~/.openclaw/credentials/.personal_webhook_secret) && curl -sf -X POST -H \"Authorization: Bearer $SECRET\" http://127.0.0.1:8791/brain/index/rebuild"],
    "graph_consolidation": [_py, f"{_bd}/brain_core/graph_consolidation.py"],
    "stale_cleanup":      [_py, f"{_bd}/brain_core/maintenance.py", "stale_cleanup"],
    "memory_observability": [_py, f"{_bd}/pipeline/memory_observability.py"],
    "lint_memory":          [_py, f"{_bd}/pipeline/lint_memory.py"],
    "near_dedup":         [_py, "-c", f"import sys; sys.path.insert(0,'{_bd}/brain_core'); from memory_lifecycle import dedup_semantic_near_duplicates; import json; print(json.dumps(dedup_semantic_near_duplicates()))"],
    "auto_resolve_contradictions": [_py, "-c", f"import sys; sys.path.insert(0,'{_bd}/brain_core'); from memory_lifecycle import auto_resolve_stale_contradictions; import json; print(json.dumps(auto_resolve_stale_contradictions()))"],
    "supersession_chain_cleanup": [_py, "-c", f"import sys; sys.path.insert(0,'{_bd}/brain_core'); from memory_lifecycle import cleanup_supersession_chains; import json; print(json.dumps(cleanup_supersession_chains()))"],
    "feedback_aggregate": [_py, f"{_bd}/brain_core/feedback_aggregator.py"],
    "entity_resolution":  [_py, f"{_bd}/pipeline/entity_resolution.py", "--apply"],
    "neo4j_backup":       [_py, f"{_bd}/cli/backup_neo4j.py"],
    # Backup (also runs via independent launchd plist as a failure-domain safety net)
    "backup":             [_py, f"{_bd}/cli/backup_chroma.py"],
    "backup_verify":      [_py, f"{_bd}/cli/backup_verify.py"],
    # reembed_migrator is manual-only (requires positional <collection> arg).
    # Invoke directly: python brain_core/pipeline/reembed_migrator.py <collection_name>
    "proactive_insights": [_py, f"{_bd}/brain_core/pipeline/proactive_linker.py"],
    "skill_extract":      [_py, f"{_bd}/brain_core/pipeline/skill_extractor.py"],
    "memory_nudge":       [_py, f"{_bd}/brain_core/pipeline/memory_nudge.py"],
    "memory_consolidation": [_py, f"{_bd}/brain_core/pipeline/memory_consolidation.py"],
    "llm_usage_purge":    [_py, f"{_bd}/brain_core/pipeline/llm_usage_purge.py"],
    "fts_rebuild":        [_py, f"{_bd}/brain_core/fts_index.py"],
    "event_compressor":   [_py, f"{_bd}/brain_core/pipeline/event_compressor.py"],
    "slo_monitor":        [_py, f"{_bd}/brain_core/slo_monitor.py"],
    "hnsw_adaptive":      [_py, f"{_bd}/brain_core/pipeline/hnsw_tuner.py", "--adaptive"],
    "memory_leak_detector": [_py, f"{_bd}/brain_core/pipeline/memory_leak_detector.py"],
    "training_pairs_generate": [_py, f"{_bd}/brain_core/pipeline/training_pair_generator.py"],
    # Round 9 — Tier 2 new pipelines
    "code_index_refresh":  [_py, f"{_bd}/ingest/code_repos.py"],
    "gap_detection":       [_py, f"{_bd}/brain_core/pipeline/gap_detector.py"],
    "trust_recompute":     [_py, "-c", f"import sys; sys.path.insert(0,'{_bd}/brain_core'); from memory_lifecycle import recompute_trust_scores; import json; print(json.dumps(recompute_trust_scores()))"],
    "focus_aggregate":     [_py, f"{_bd}/brain_core/pipeline/focus_aggregator.py"],
    # Round 10 Wave 2 — episodic memory binding (CoALA-style)
    "episode_binder":      [_py, f"{_bd}/brain_core/pipeline/episode_binder.py"],
    # Round 10 Wave 3 — synaptic pruning of atrophied memories (MemoryBank)
    "memory_pruning":      [_py, "-c", f"import sys; sys.path.insert(0,'{_bd}/brain_core'); from memory_lifecycle import prune_atrophied_memories; import json; print(json.dumps(prune_atrophied_memories(dry_run=True)))"],
    "memory_pruning_active": [_py, "-c", f"import sys; sys.path.insert(0,'{_bd}/brain_core'); from memory_lifecycle import prune_atrophied_memories; import json; print(json.dumps(prune_atrophied_memories(dry_run=False, max_age_days=120, compress_with_gist=True)))"],
    "stale_superseded_cleanup": [_py, "-c", f"import sys; sys.path.insert(0,'{_bd}/brain_core'); from memory_lifecycle import cleanup_stale_superseded; import json; print(json.dumps(cleanup_stale_superseded()))"],
    # LoRA fine-tuning — manual trigger only, behind BRAIN_FINETUNE_ENABLED flag.
    # Must run in the brain venv since sentence-transformers/peft/torch are only
    # installed there, not in the system Python.
    "embed_finetune":     [f"{_bd}/.venv/bin/python3", f"{_bd}/cli/brain_finetune.py"],
    # Infra validation + health reports
    "infra_validation":     [_py, f"{_bd}/brain_core/maintenance.py", "validate_infra"],
    "memory_health_report": [_py, "-c", f"import sys; sys.path.insert(0,'{_bd}/brain_core'); from memory_lifecycle import memory_health_report; import json; print(json.dumps(memory_health_report()))"],
    "content_quality_slo":  [_py, "-c", f"import sys; sys.path.insert(0,'{_bd}/brain_core'); from slo_monitor import check_content_quality; import json; print(json.dumps(check_content_quality()))"],
}

_running_jobs: dict[str, subprocess.Popen] = {}
_running_jobs_lock = threading.Lock()
_CRITICAL_JOBS = {"personal_ingest", "backup", "canonical_pipeline", "reindex"}

SERVER_START = time.time()


# ── Pydantic models ─────────────────────────────────────
class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str = "brain-server"
    port: int = LISTEN_PORT
    uptime_sec: int


class MetricsResponse(BaseModel):
    collection_counts: dict[str, int]
    total_chunks: int
    uptime_sec: int
    profile_loaded: bool
    routes: dict[str, Any] = Field(default_factory=dict)
    jobs: dict[str, Any] = Field(default_factory=dict)
    dispatch: dict[str, Any] = Field(default_factory=dict)
    memory_writes_1h: int = 0
    scheduler_next_runs: dict[str, str] = Field(default_factory=dict)
    contradiction_queue_depth: int = 0
    last_learn_success_at: str = ""
    last_backup_at: str = ""
    last_backup_ok: bool = True
    embed_cache: dict[str, Any] = Field(default_factory=dict)


class CaptureRequest(BaseModel):
    """Generic capture payload — wrapped into a schema-compliant raw record on write."""
    event: str | None = None
    place: str | None = None
    lat: float | None = None
    lon: float | None = None
    accuracy: float | None = None
    battery: float | None = None
    # iOS HealthKit fields
    sleep_hrs: float | None = None
    sleep_quality: str | None = None
    steps: int | None = None
    hrv_avg: float | None = None
    rest_hr: float | None = None
    workouts_count: int | None = None
    # Free-form passthrough so iOS Shortcuts can send arbitrary keys
    extra: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "allow"}


class CaptureResponse(BaseModel):
    status: Literal["ok"] = "ok"
    stored: str
    kind: str


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


class ThinkRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=1000)
    context: str | None = Field(default=None, max_length=2000)


class ThinkProvenance(BaseModel):
    id: str
    title: str
    source: str
    snippet: str


class ThinkResponse(BaseModel):
    question: str
    answer: str
    provenance: list[ThinkProvenance] = Field(default_factory=list)
    model: str = "jenna"
    latency_ms: int = 0


# ── Decision / reasoning models ─────────────────────────
class DecideRequest(BaseModel):
    situation: str = Field(..., min_length=10, max_length=2000)
    options: list[dict] = Field(..., min_length=2, max_length=6)
    agent: str = Field(default="claude", max_length=32)
    domain: str | None = Field(default=None)
    context: str | None = Field(default=None, max_length=2000)


class DecideResponse(BaseModel):
    situation: str
    recommendation: str
    reasoning: str
    confidence: float
    evidence: list[dict] = Field(default_factory=list)
    exceptions: list[str] = Field(default_factory=list)
    model: str = "sage"
    latency_ms: int = 0
    cached: bool = False
    heuristic_fallback: bool = False


class ReasonRequest(BaseModel):
    question: str = Field(..., min_length=5, max_length=2000)
    context: str | None = Field(default=None, max_length=3000)
    agent: str = Field(default="claude", max_length=32)
    domain: str | None = None


class ReasonResponse(BaseModel):
    question: str
    analysis: str
    reasoning_steps: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    provenance: list[dict] = Field(default_factory=list)
    model: str = "sage"
    latency_ms: int = 0


# ── Autonomy models ────────────────────────────────────
class AutopilotRequest(BaseModel):
    enabled: bool
    confidence_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    updated_by: str = Field(default="api", max_length=32)


class TaskCreateRequest(BaseModel):
    title: str = Field(..., min_length=3, max_length=200)
    description: str = Field(default="", max_length=5000)
    assigned_agent: str | None = None
    priority: int = Field(default=5, ge=1, le=10)
    parent_goal_id: str | None = None
    confidence: float | None = None
    metadata: dict = Field(default_factory=dict)


class GoalCreateRequest(BaseModel):
    title: str = Field(..., min_length=3, max_length=200)
    description: str = Field(default="", max_length=5000)
    auto_decompose: bool = True


class FocusRequest(BaseModel):
    content: str = Field(..., min_length=3, max_length=500)
    category: str = Field(default="focus")
    agent: str | None = None
    expires_hours: int = Field(default=168, ge=1, le=720)


# ── Self-learning + memory CRUD models ─────────────────
class LearnRequest(BaseModel):
    transcript: str = Field(..., min_length=10, max_length=50_000)
    source: str = Field(default="session", max_length=64)
    agent: str = Field(default="claude", max_length=32)


class LearnResponse(BaseModel):
    status: Literal["queued", "ok"] = "queued"
    candidates: int = 0
    message: str = "processing in background"


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


class BrainIngestRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=50000)
    source: str = Field(default="api")
    category: str = Field(default="other")
    tags: list[str] = Field(default_factory=list)


# ── Caches ──────────────────────────────────────────────
class ProfileCache:
    def __init__(self, paths: list[Path] | Path, ttl_seconds: int = 60):
        # Accept single Path (legacy) or list of Paths (identity + state split).
        self.paths: list[Path] = paths if isinstance(paths, list) else [paths]
        self.ttl = ttl_seconds
        self._lock = threading.Lock()
        self._content: str | None = None
        self._mtimes: tuple[float, ...] = ()
        self._last_check: float = 0.0

    def get(self) -> str | None:
        with self._lock:
            now = time.time()
            if self._content is not None and (now - self._last_check) < self.ttl:
                return self._content
            existing = [p for p in self.paths if p.exists()]
            if not existing:
                return None
            current_mtimes = tuple(p.stat().st_mtime for p in existing)
            if self._content is None or current_mtimes != self._mtimes:
                parts = [p.read_text() for p in existing]
                self._content = "\n\n".join(parts)
                self._mtimes = current_mtimes
            self._last_check = now
            return self._content

    def section(self, name: str) -> str | None:
        full = self.get()
        if not full:
            return None
        target = name.replace("_", " ").lower()
        out_lines: list[str] = []
        capturing = False
        # Walk every line; strip frontmatter blocks inline so concatenated files parse correctly.
        in_frontmatter = False
        for line in full.splitlines():
            stripped = line.strip()
            if stripped.startswith("---"):
                in_frontmatter = not in_frontmatter
                continue
            if in_frontmatter:
                continue
            if stripped.startswith("## "):
                if capturing:
                    break
                if stripped[3:].strip().lower().startswith(target):
                    capturing = True
                    out_lines.append(line)
                    continue
            if capturing:
                out_lines.append(line)
        return "\n".join(out_lines).strip() if out_lines else None


_profile_cache = ProfileCache([IDENTITY_FILE, STATE_FILE], ttl_seconds=PROFILE_CACHE_TTL)


# ── Helpers ─────────────────────────────────────────────
_cached_secret: str | None = None


def _load_secret() -> str | None:
    if not SECRET_FILE.exists():
        return None
    return SECRET_FILE.read_text().strip()


def _log_failure(reason: str, route: str = "?") -> None:
    try:
        FAILURE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with FAILURE_LOG.open("a") as f:
            f.write(json.dumps({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "route": route,
                "reason": reason[:500],
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _get_collection_counts() -> dict[str, int]:
    """Collection counts via direct HTTP to ChromaDB (no docker exec)."""
    try:
        cols = _chroma_api("GET", "/api/v2/tenants/default_tenant/databases/default_database/collections")
    except Exception as e:
        return {"_error": str(e)[:200]}
    if not isinstance(cols, list):
        return {"_error": "unexpected chroma response"}
    counts: dict[str, int] = {}
    for c in cols:
        cid = c.get("id")
        name = c.get("name")
        if not cid or not name:
            continue
        try:
            cnt = _chroma_api(
                "GET",
                f"/api/v2/tenants/default_tenant/databases/default_database/collections/{cid}/count",
            )
            counts[name] = int(cnt) if isinstance(cnt, (int, str)) else -1
        except Exception:
            counts[name] = -1
    return counts


def _build_raw_record(source_type: str, payload: dict) -> dict:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    iso = now.isoformat().replace("+00:00", "Z")
    content_str = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(content_str.encode()).hexdigest()
    date_part = iso[:10].replace("-", "_")
    rec_id = f"raw_{source_type}_{date_part}_{digest[:8]}"

    entities = ["Chris"]
    if isinstance(payload.get("place"), str):
        entities.append(payload["place"])

    return {
        "id": rec_id,
        "timestamp": iso,
        "source_type": source_type,
        "source_ref": f"brain-api:{payload.get('event', source_type)}",
        "actor": "chris",
        "visibility": "private",
        "scrub_status": "scrubbed",
        "content": content_str,
        "attachments": [],
        "entities": entities,
        "hash": f"sha256:{digest}",
    }


def _write_inbox(source_type: str, payload: dict) -> Path:
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    record = _build_raw_record(source_type, payload)
    out = INBOX_DIR / f"{record['id']}.json"
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2))
    return out


# ── Auth dependency ─────────────────────────────────────
def verify_bearer(authorization: Annotated[str | None, Header()] = None) -> None:
    """Auth dependency injected into every protected route. /healthz and /docs skip this."""
    secret = _cached_secret
    if not secret:
        _log_failure("server has no secret configured")
        raise HTTPException(status_code=503, detail="server misconfigured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    provided = authorization[len("Bearer "):].strip()
    if not hmac.compare_digest(provided, secret):
        raise HTTPException(status_code=401, detail="invalid bearer token")


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
            from search import get_embedding, get_collections
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
        brain_scheduler.start(_dispatch_job)
    except Exception as e:
        _log_failure(f"scheduler start failed: {e}", route="lifespan")
    global _cached_secret
    _cached_secret = _load_secret()
    _prewarm_caches()
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
    contact={"name": "Chris Cho", "email": "wheogus98@gmail.com"},
    lifespan=lifespan,
)

_cors_origins = os.getenv("BRAIN_CORS_ORIGINS", "").strip()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins.split(",") if _cors_origins else [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8791",
        "http://127.0.0.1:8791",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)


# ── Request latency middleware ──────────────────────────
@app.middleware("http")
async def _metrics_middleware(request, call_next):
    t0 = time.time()
    error = False
    try:
        response = await call_next(request)
        if response.status_code >= 500:
            error = True
    except Exception:
        error = True
        raise
    finally:
        latency_ms = (time.time() - t0) * 1000
        _metrics_buf.record_request(str(request.url.path), latency_ms, error=error)
    return response


# ── Routes: liveness ────────────────────────────────────
@app.get("/healthz", response_model=HealthResponse, tags=["liveness"])
def healthz() -> HealthResponse:
    """Liveness probe — no auth required."""
    return HealthResponse(uptime_sec=int(time.time() - SERVER_START))


# ── Routes: metrics ─────────────────────────────────────
@app.get("/metrics", response_model=MetricsResponse, tags=["metrics"], dependencies=[Depends(verify_bearer)])
def metrics() -> MetricsResponse:
    counts = _get_collection_counts()
    total = sum(c for c in counts.values() if isinstance(c, int) and c >= 0)
    buf = _metrics_buf.snapshot()

    # Next-run times from the scheduler
    next_runs: dict[str, str] = {}
    try:
        for j in brain_scheduler.list_jobs():
            if j.get("next_run"):
                next_runs[j["name"]] = j["next_run"]
    except Exception:
        pass

    contradiction_depth = counts.get("semantic_contradictions", 0)
    if not isinstance(contradiction_depth, int) or contradiction_depth < 0:
        contradiction_depth = 0

    # Embedding cache stats
    try:
        from embed_cache import cache_stats as _embed_stats
        embed_cache = _embed_stats()
    except Exception:
        embed_cache = {}

    return MetricsResponse(
        collection_counts=counts,
        total_chunks=total,
        uptime_sec=int(time.time() - SERVER_START),
        profile_loaded=_profile_cache.get() is not None,
        routes=buf["routes"],
        jobs=buf["jobs"],
        dispatch=buf["dispatch"],
        memory_writes_1h=buf["memory_writes_1h"],
        scheduler_next_runs=next_runs,
        contradiction_queue_depth=contradiction_depth,
        last_learn_success_at=buf.get("last_learn_success_at", ""),
        last_backup_at=buf.get("last_backup_at", ""),
        last_backup_ok=buf.get("last_backup_ok", True),
        embed_cache=embed_cache,
    )


@app.get("/collections", tags=["metrics"], dependencies=[Depends(verify_bearer)])
def collections() -> dict[str, int]:
    return _get_collection_counts()


# ── Routes: profile ─────────────────────────────────────
@app.get("/profile", response_class=PlainTextResponse, tags=["profile"], dependencies=[Depends(verify_bearer)])
def profile() -> str:
    content = _profile_cache.get()
    if content is None:
        raise HTTPException(status_code=404, detail="profile not found")
    return content


@app.get("/profile/section/{name}", response_class=PlainTextResponse, tags=["profile"], dependencies=[Depends(verify_bearer)])
def profile_section(name: Annotated[str, PathParam()]) -> str:
    content = _profile_cache.section(name)
    if content is None:
        raise HTTPException(status_code=404, detail=f"section '{name}' not found")
    return content


# ── Routes: recall ──────────────────────────────────────
@app.get("/recall", response_model=RecallResponse, tags=["recall"], dependencies=[Depends(verify_bearer)])
def recall(
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
    _filter_free = not any((since, until, entity, collection, domain, source_type,
                            include_history, include_obsolete, as_of))
    if _filter_free:
        cached = _recall_emb_cache_lookup(q)
        if cached is not None:
            # Round 10 C1: still reinforce semantic_memory hits even on cache
            # hit — the user is "accessing" those memories regardless of where
            # the response comes from. Fire-and-forget so cache lookups stay fast.
            try:
                cached_results = cached.get("results", []) if isinstance(cached, dict) else []
                cached_sem_ids = []
                for r in cached_results:
                    if not isinstance(r, dict):
                        continue
                    col = r.get("collection") or ""
                    if col != "semantic_memory" and "semantic" not in col:
                        continue
                    rid = r.get("id") or (r.get("metadata") or {}).get("id")
                    if rid:
                        cached_sem_ids.append(rid)
                    if len(cached_sem_ids) >= 5:
                        break
                if cached_sem_ids:
                    from brain_core.memory_lifecycle import reinforce_on_access
                    from brain_core.search_unified import _search_bg_pool
                    _search_bg_pool.submit(reinforce_on_access, cached_sem_ids)
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
        q, search_n, sources=["rag", "canonical", "obsidian"],
        domain=domain, original_query=q, where=where,
        collections=collections_arg, entity=entity, explain=False,
        source_type=source_type,
        include_history=include_history,
        include_obsolete=include_obsolete,
        as_of=as_of,
    )
    if (start_dt or end_dt) and isinstance(payload, dict):
        payload["results"] = temporal.filter_by_created_at(
            payload.get("results", []), start_dt, end_dt
        )[:n]
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
        sem_ids = []
        for r in results_list:
            if not isinstance(r, dict):
                continue
            col = r.get("collection") or ""
            if col != "semantic_memory" and "semantic" not in col:
                continue
            rid = r.get("id") or (r.get("metadata") or {}).get("id")
            if rid:
                sem_ids.append(rid)
            if len(sem_ids) >= 5:
                break
        if sem_ids:
            from brain_core.memory_lifecycle import reinforce_on_access
            from brain_core.search_unified import _search_bg_pool
            _search_bg_pool.submit(reinforce_on_access, sem_ids)
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
_recall_embedding_cache: list[tuple[float, list[float], str, dict]] = []  # (timestamp, embedding, query, response)
_RECALL_EMB_TTL = 60.0
_RECALL_EMB_MAX = 50
_RECALL_EMB_SIM_THRESHOLD = 0.92


def _cosine(a: list[float], b: list[float]) -> float:
    import math
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
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
    now = time.time()
    # Snapshot under lock, scan outside. The cosine loop is O(N*dim) ~50k
    # float mults and must not run inside a contention hotspot.
    with _recall_emb_lock:
        _recall_embedding_cache[:] = [e for e in _recall_embedding_cache if now - e[0] < _RECALL_EMB_TTL]
        snapshot = list(_recall_embedding_cache)
    for _ts, cached_emb, _cached_query, resp in snapshot:
        if _cosine(emb, cached_emb) > _RECALL_EMB_SIM_THRESHOLD:
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
    now = time.time()
    with _recall_emb_lock:
        _recall_embedding_cache.append((now, emb, query, response))
        if len(_recall_embedding_cache) > _RECALL_EMB_MAX:
            _recall_embedding_cache.pop(0)


# ── Routes: recall v2 (HyDE + expand + rerank + time-decay + RRF) ──
_auto_feedback_count = 0
_auto_feedback_hour = 0  # hour (unix ts // 3600) of last reset
_AUTO_FEEDBACK_MAX_PER_HOUR = 100


def _record_auto_feedback(query: str, results: list[dict], agent: str) -> None:
    """Write served-result feedback events to search-feedback.jsonl. Rate-limited."""
    global _auto_feedback_count, _auto_feedback_hour
    now = datetime.now(timezone.utc)
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
    sem_ids: list[str] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        rid = r.get("id") or r.get("path") or (r.get("metadata") or {}).get("id") or ""
        col = r.get("collection") or ""
        is_sem = col == "semantic_memory" or "semantic" in col
        lines.append(json.dumps({
            "query": query[:500],
            "result_id": rid,
            "result_source": col,
            "score": 0.7 if is_sem else None,
            "served": True,
            "timestamp": ts,
            "agent": agent,
        }))
        if is_sem and rid:
            sem_ids.append(rid)
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
    # Reinforce semantic_memory hits (same as /recall v1 does)
    if sem_ids:
        try:
            from brain_core.memory_lifecycle import reinforce_on_access
            from brain_core.search_unified import _search_bg_pool
            _search_bg_pool.submit(reinforce_on_access, sem_ids[:5])
        except Exception:
            pass


@app.get("/recall/v2", response_model=RecallV2Response, tags=["recall"], dependencies=[Depends(verify_bearer)])
def recall_v2(
    request: Request,
    q: str,
    n: int = Query(default=10, ge=1, le=50),
    hyde: bool = False,
    expand: bool = False,
    rerank: bool = True,
    decay: bool = True,
    since: str | None = None,
    until: str | None = None,
    entity: str | None = None,
    collection: str | None = None,
    domain: str | None = None,
    source_type: str | None = Query(default=None, max_length=32),
    include_history: bool = Query(default=False),
    include_obsolete: bool = Query(default=False),
    as_of: str | None = Query(default=None, max_length=20),
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
    """
    if not q.strip():
        raise HTTPException(status_code=400, detail="q parameter required")

    # Response cache — identical queries within 30s return cached
    cache_key = f"{q}:{n}:{hyde}:{expand}:{rerank}:{decay}:{collection}:{domain}:{since}:{until}:{entity}:{source_type}:{include_history}:{include_obsolete}:{as_of}"
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
    from concurrent.futures import ThreadPoolExecutor as _VariantPool, as_completed as _as_completed
    def _run_variant(v_query):
        return search_unified.search_all(
            v_query, n * search_n_mult,
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
                    hypothetical, n * search_n_mult,
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
                p["results"] = temporal.filter_by_created_at(
                    p["results"], start_dt, end_dt
                )

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
        # Stage 1: token-overlap + trust/source boosts.
        # search_all already runs this once per variant. For single-variant
        # queries (the common case — `expand=False` default), the RRF step
        # is a no-op fuse and these scores are already final, so re-running
        # rerank is pure duplicate work (~5–10ms per recall). Skip if the
        # fused list already carries rerank_score from search_all.
        already_reranked = bool(fused) and all("rerank_score" in r for r in fused)
        if already_reranked and len(variants) == 1:
            for r in fused:
                r["score"] = r.get("rerank_score", r.get("score", 0))
        else:
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
                # top_k=20 is sufficient for n≤10 responses — the tail was
                # wasted CE compute (~60% overhead per review).
                fused = rerank_with_cross_encoder(q, fused, top_k=20)
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
    _enrichable_types = {"canonical-note", "distilled-note", "obsidian-note",
                         "agent-config", "learning", "docker-compose", "nginx-conf"}
    for _r in fused[:n]:
        _path = _r.get("path", "")
        if not _path or _path in _seen_paths:
            continue
        _rtype = (_r.get("type") or (_r.get("metadata") or {}).get("type") or "")
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

    total_candidates = sum(p.get("total_candidates", 0) for p in all_payloads)
    timing["total_ms"] = int((time.time() - t_start) * 1000)
    timing["result_count"] = min(n, len(fused))
    timing["candidate_count"] = total_candidates

    _metrics_buf.record_search_latency(timing["total_ms"], timing)

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
        filter_free = not (collection or domain or entity or source_type or since or until or as_of
                           or include_history or include_obsolete)
        if filter_free:
            results_list = fused[:n]
            ce_scores = [float(r.get("cross_encoder_score", 0)) for r in results_list if r.get("cross_encoder_score") is not None]
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
                    gf.write(json.dumps({
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "query": q[:500],
                        "n_results": len(results_list),
                        "max_score": round(max_score, 2),
                        "max_ce_score": round(max_ce, 4) if ce_scores else None,
                        "endpoint": "/recall/v2",
                    }) + "\n")
    except Exception:
        pass

    # Auto-record search feedback for served results (fire-and-forget).
    agent = request.headers.get("x-agent") or "unknown"
    if background is not None:
        background.add_task(_record_auto_feedback, q, fused[:n], agent)
    else:
        # Fallback: fire-and-forget via thread pool
        try:
            from brain_core.search_unified import _search_bg_pool
            _search_bg_pool.submit(_record_auto_feedback, q, fused[:n], agent)
        except Exception:
            pass

    return response


@app.post("/recall/feedback", tags=["recall"], dependencies=[Depends(verify_bearer)])
def search_feedback(req: SearchFeedbackRequest):
    """Record user feedback on search results. Reinforces memory via MemRL."""
    try:
        feedback_log = BRAIN_DIR / "logs" / "search-feedback.jsonl"
        feedback_log.parent.mkdir(parents=True, exist_ok=True)
        with feedback_log.open("a") as f:
            f.write(json.dumps({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "query": req.query,
                "result_id": req.result_id,
                "source": req.result_source,
                "useful": req.useful,
                "agent": req.agent,
            }) + "\n")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"feedback log write failed: {e}")

    # Reinforce memory if it's a semantic_memory result
    if req.result_id.startswith("semantic_memory:"):
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


# ── Routes: /brain/reason/multihop — LangGraph-style multi-hop reasoning ──
class MultiHopReasonRequest(BaseModel):
    question: str = Field(..., min_length=5, max_length=1000)
    max_hops: int = Field(default=5, ge=1, le=5)


@app.post("/brain/reason/multihop", tags=["recall"], dependencies=[Depends(verify_bearer)])
def brain_reason_multihop(req: MultiHopReasonRequest):
    """Multi-hop reasoning with LangGraph-style checkpoints."""
    try:
        import reasoning_loop
        result = reasoning_loop.run_reasoning(req.question, max_hops=req.max_hops)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"reasoning failed: {e}")


@app.post("/brain/reason/multihop/{thread_id}/resume", tags=["recall"], dependencies=[Depends(verify_bearer)])
def brain_reason_multihop_resume(thread_id: Annotated[str, PathParam()]):
    """Resume a reasoning thread from last checkpoint."""
    try:
        import reasoning_loop
        return reasoning_loop.resume_reasoning(thread_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"resume failed: {e}")


# ── Routes: /chris/think — decision endpoint in Chris's first-person voice ──
CHRIS_THINK_PROMPT = """You ARE Chris. Answer in first-person, direct, dry, no flattery. Match Chris's voice from his profile. Pretend you are his inner voice. No preamble, no "As Chris, I would". Just answer the question as if you're thinking out loud.

Chris's profile:
{profile}

Relevant recent preferences / facts / decisions:
{memories}

Recent context / schedule:
{context}

{extra_context}

Question: {question}

Your answer (one or two short paragraphs, first-person, no preamble):"""


def _compose_think_prompt(question: str, extra_context: str | None) -> tuple[str, list[dict]]:
    """Build the prompt + return the provenance list of memories cited."""
    # 1. Profile sections
    profile_parts: list[str] = []
    for section in ("identity", "hard rules", "values", "tools", "workflow"):
        text = _profile_cache.section(section)
        if text:
            profile_parts.append(text.strip())
    profile_text = "\n\n".join(profile_parts)[:3000] or "(profile unavailable)"

    # 2. Relevant memories — search semantic_memory for the question, rerank.
    provenance: list[dict] = []
    memory_lines: list[str] = []
    try:
        sm_payload = search_unified.search_all(
            question, 8,
            sources=["rag", "canonical"],
            collections=["semantic_memory"],
            original_query=question,
        )
        rag_payload = search_unified.search_all(
            question, 6,
            sources=["rag", "canonical"],
            original_query=question,
        )
        # Fuse + rerank for best-of.
        merged = _rrf.rrf_fuse(
            [sm_payload.get("results", []), rag_payload.get("results", [])],
            id_key="path",
        )
        merged = _rerank.rerank(question, merged, top_k=6)
        for m in merged:
            content = (m.get("content") or "")[:250]
            title = m.get("title") or m.get("collection") or ""
            memory_lines.append(f"- {content}")
            provenance.append({
                "id": m.get("path") or m.get("metadata", {}).get("id", ""),
                "title": title[:120],
                "source": m.get("collection", ""),
                "snippet": content[:200],
            })
    except Exception:
        pass
    memories_text = "\n".join(memory_lines) or "(no relevant memories found)"

    # 3. Schedule / calendar context (best-effort)
    context_lines: list[str] = []
    try:
        cal_payload = search_unified.search_all(
            question, 3,
            sources=["rag"],
            collections=["personal"],
            original_query=question,
        )
        for c in cal_payload.get("results", []):
            context_lines.append(f"- {c.get('content','')[:200]}")
    except Exception:
        pass
    context_text = "\n".join(context_lines) or "(no calendar context)"

    extra_text = f"Additional context from caller:\n{extra_context}" if extra_context else ""

    prompt = CHRIS_THINK_PROMPT.format(
        profile=profile_text,
        memories=memories_text,
        context=context_text,
        extra_context=extra_text,
        question=question,
    )
    return prompt, provenance


# In-memory cache for /chris/think (60s TTL — same question twice returns cached).
_think_cache: dict[str, tuple[float, ThinkResponse]] = {}
_think_cache_lock = threading.Lock()
_THINK_CACHE_TTL = 60


@app.post("/chris/think", response_model=ThinkResponse, tags=["decide"], dependencies=[Depends(verify_bearer)])
def chris_think(req: ThinkRequest) -> ThinkResponse:
    """Ask Chris's second brain a decision question. Answers in first-person voice.

    Pipeline:
      1. Pull profile sections (identity, hard rules, values, tools, workflow)
      2. Search semantic_memory + canonical for relevant preferences/decisions
      3. Lookup calendar context if the question mentions scheduling
      4. Compose prompt → dispatch to Jenna (OpenAI via OpenClaw)
      5. Return answer + provenance trail

    No direct LLM calls — all inference goes through `openclaw agent --agent jenna`.
    """
    cache_key = f"{req.question}||{req.context or ''}"
    # TTL check must happen INSIDE the lock — otherwise two concurrent
    # callers with the same question both see "no cache entry", both
    # dispatch Jenna (90s each, billable), and one overwrites the other.
    with _think_cache_lock:
        cached = _think_cache.get(cache_key)
        if cached and (time.time() - cached[0]) < _THINK_CACHE_TTL:
            return cached[1]

    t_start = time.time()
    prompt, provenance = _compose_think_prompt(req.question, req.context)

    dispatch_result = _openclaw_dispatch(
        agent="jenna",
        message=prompt,
        thinking="medium",
        timeout=90,
    )

    _metrics_buf.record_dispatch(
        ok=dispatch_result.ok,
        duration_ms=dispatch_result.duration_ms,
        rate_limited=dispatch_result.rate_limited,
        auth_failed=dispatch_result.auth_failed,
        attempts=dispatch_result.attempts,
    )

    if not dispatch_result.ok:
        detail = f"openclaw dispatch failed: {dispatch_result.error}"
        if dispatch_result.rate_limited:
            raise HTTPException(status_code=503, detail=f"rate_limited: {detail}")
        if dispatch_result.auth_failed:
            raise HTTPException(status_code=502, detail=f"auth_failed: {detail}")
        raise HTTPException(status_code=502, detail=detail)

    answer = dispatch_result.text.strip()
    if not answer:
        raise HTTPException(status_code=502, detail="openclaw returned empty answer")

    response = ThinkResponse(
        question=req.question,
        answer=answer,
        provenance=[ThinkProvenance(**p) for p in provenance[:6]],
        model=dispatch_result.model or "jenna",
        latency_ms=int((time.time() - t_start) * 1000),
    )

    with _think_cache_lock:
        _think_cache[cache_key] = (time.time(), response)
        if len(_think_cache) > 64:
            oldest = min(_think_cache, key=lambda k: _think_cache[k][0])
            _think_cache.pop(oldest, None)

    return response


@app.get("/boot-context/{agent}", tags=["recall"], dependencies=[Depends(verify_bearer)])
def boot_ctx(agent: Annotated[str, PathParam()], n: int = 3) -> dict:
    sections = boot_context.build_boot_context(agent, n)
    return {"agent": agent, "sections": sections}


@app.post("/boot-context/flush", tags=["recall"], dependencies=[Depends(verify_bearer)])
def boot_ctx_flush() -> dict:
    boot_context.flush_cache()
    return {"status": "ok", "message": "boot context cache flushed"}


# ── Routes: synthesis read ──────────────────────────────
# Validation patterns for synthesis target params. These prevent path
# traversal: the target is interpolated into a file path, so any "..", "/",
# or unexpected char could let an authenticated caller read files outside
# the synthesis dir (e.g., canonical profile).
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WEEK_RE = re.compile(r"^\d{4}-W\d{2}$")
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


@app.get("/synthesis/daily", response_class=PlainTextResponse, tags=["synthesis"], dependencies=[Depends(verify_bearer)])
def synthesis_daily(date: str | None = None) -> str:
    target = date or datetime.now().strftime("%Y-%m-%d")
    if not _DATE_RE.match(target):
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    f = DISTILLED_DAILY / f"{target}.md"
    if not f.exists():
        raise HTTPException(status_code=404, detail=f"no daily synthesis for {target}")
    return f.read_text()


@app.get("/synthesis/weekly", response_class=PlainTextResponse, tags=["synthesis"], dependencies=[Depends(verify_bearer)])
def synthesis_weekly(week: str | None = None) -> str:
    target = week or datetime.now().strftime("%G-W%V")
    if not _WEEK_RE.match(target):
        raise HTTPException(status_code=400, detail="week must be YYYY-Www")
    f = WEEKLY_DIR / f"{target}.md"
    if not f.exists():
        raise HTTPException(status_code=404, detail=f"no weekly arc for {target}")
    return f.read_text()


@app.get("/synthesis/monthly", response_class=PlainTextResponse, tags=["synthesis"], dependencies=[Depends(verify_bearer)])
def synthesis_monthly(month: str | None = None) -> str:
    target = month or datetime.now().strftime("%Y-%m")
    if not _MONTH_RE.match(target):
        raise HTTPException(status_code=400, detail="month must be YYYY-MM")
    f = MONTHLY_DIR / f"{target}.md"
    if not f.exists():
        raise HTTPException(status_code=404, detail=f"no monthly arc for {target}")
    return f.read_text()


# ── Routes: capture (POST) ──────────────────────────────
@app.post("/location/ingest", response_model=CaptureResponse, tags=["capture"], dependencies=[Depends(verify_bearer)])
@app.post("/location", response_model=CaptureResponse, tags=["capture"], include_in_schema=False, dependencies=[Depends(verify_bearer)])
def capture_location(payload: CaptureRequest) -> CaptureResponse:
    data = payload.model_dump(exclude_none=True)
    data["_received_at"] = datetime.now(timezone.utc).isoformat()
    out = _write_inbox("location", data)
    return CaptureResponse(stored=out.name, kind="location")


@app.post("/health/ingest", response_model=CaptureResponse, tags=["capture"], dependencies=[Depends(verify_bearer)])
@app.post("/health", response_model=CaptureResponse, tags=["capture"], include_in_schema=False, dependencies=[Depends(verify_bearer)])
def capture_health(payload: CaptureRequest) -> CaptureResponse:
    data = payload.model_dump(exclude_none=True)
    data["_received_at"] = datetime.now(timezone.utc).isoformat()
    out = _write_inbox("health", data)
    return CaptureResponse(stored=out.name, kind="health")


@app.post("/capture/{source_type}", response_model=CaptureResponse, tags=["capture"], dependencies=[Depends(verify_bearer)])
def capture_generic(source_type: Annotated[str, PathParam()], payload: CaptureRequest) -> CaptureResponse:
    if not source_type or not re.fullmatch(r'[a-z0-9_\-]{1,32}', source_type):
        raise HTTPException(status_code=400, detail="source_type must be 1-32 chars of [a-z0-9_-]")
    data = payload.model_dump(exclude_none=True)
    data["_received_at"] = datetime.now(timezone.utc).isoformat()
    out = _write_inbox(source_type, data)
    return CaptureResponse(stored=out.name, kind=source_type)


# ── Jobs: shared dispatcher (used by POST /jobs/{name} and the scheduler) ──
def _dispatch_job(job_name: str) -> int:
    """Fire-and-forget launch of a registered job. Returns the child PID.

    Holds _running_jobs_lock across the full check-and-spawn to prevent a
    TOCTOU race where two concurrent dispatches (e.g. scheduler + manual POST)
    both pass the "already running" guard and spawn duplicate subprocesses
    for critical jobs like backup or canonical_pipeline.
    """
    cmd = JOB_REGISTRY.get(job_name)
    if not cmd:
        raise ValueError(f"unknown job '{job_name}'")
    log_dir = BRAIN_DIR / "logs" / "jobs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / f"{job_name}.log"
    stderr_path = log_dir / f"{job_name}.err.log"
    stdout_f = None
    stderr_f = None
    try:
        stdout_f = stdout_path.open("ab")
        stderr_f = stderr_path.open("ab")
        with _running_jobs_lock:
            existing = _running_jobs.get(job_name)
            if existing and existing.poll() is None:
                raise ValueError(f"job '{job_name}' is already running (pid={existing.pid})")
            proc = subprocess.Popen(
                cmd,
                stdout=stdout_f,
                stderr=stderr_f,
                start_new_session=True,
            )
            _running_jobs[job_name] = proc
        threading.Thread(target=_wait_for_job, args=(job_name, proc, stderr_path), daemon=True).start()
        return proc.pid
    except Exception as e:
        _metrics_buf.record_job_result(job_name, ok=False, error=str(e))
        raise
    finally:
        if stdout_f:
            stdout_f.close()
        if stderr_f:
            stderr_f.close()


def _wait_for_job(job_name: str, proc: subprocess.Popen, stderr_path: Path) -> None:
    """Background thread: wait for job completion, record exit code, alert on failure."""
    try:
        proc.wait(timeout=3600)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

    exit_code = proc.returncode
    error_msg = None
    if exit_code != 0:
        try:
            error_msg = stderr_path.read_text()[-500:] if stderr_path.exists() else f"exit code {exit_code}"
        except Exception:
            error_msg = f"exit code {exit_code}"

    _metrics_buf.record_job_result(job_name, ok=(exit_code == 0), error=error_msg or "")

    if exit_code == 0 and "backup" in job_name:
        _metrics_buf.record_backup_result(True)

    if exit_code == 0 and job_name == "profile_regen":
        try:
            boot_context.flush_cache()
        except Exception:
            pass

    if exit_code != 0 and job_name in _CRITICAL_JOBS:
        try:
            _openclaw_dispatch(
                agent="jenna",
                message=f"[BRAIN ALERT] Job '{job_name}' failed with exit code {exit_code}. Error: {(error_msg or '')[:200]}",
                thinking="off",
                timeout=30,
            )
        except Exception:
            pass

    # Record completion time in scheduler history
    pending = brain_scheduler._pending_completions.pop(job_name, None)
    if pending:
        start_ts, row_id = pending
        brain_scheduler.record_completion(job_name, row_id, start_ts, error_msg)

    with _running_jobs_lock:
        _running_jobs.pop(job_name, None)


# ── Routes: jobs (fire-and-forget + scheduler surface) ─
@app.get("/jobs", tags=["jobs"], dependencies=[Depends(verify_bearer)])
def list_jobs() -> dict:
    """List every registered job with its scheduler state + recent history."""
    return {
        "registry": sorted(JOB_REGISTRY.keys()),
        "scheduler": brain_scheduler.list_jobs(),
    }


@app.get("/jobs/{job}/history", tags=["jobs"], dependencies=[Depends(verify_bearer)])
def job_history(job: Annotated[str, PathParam()]) -> dict:
    if job not in JOB_REGISTRY:
        raise HTTPException(status_code=404, detail=f"unknown job '{job}'")
    return {"job": job, "history": brain_scheduler.get_history(job)}


@app.post("/jobs/{job}", response_model=JobResponse, tags=["jobs"], dependencies=[Depends(verify_bearer)])
def trigger_job(job: Annotated[str, PathParam()]) -> JobResponse:
    """Manually trigger a job now. Records in scheduler history."""
    try:
        pid = brain_scheduler.trigger_now(job) if getattr(brain_scheduler, '_dispatcher', None) else _dispatch_job(job)
    except ValueError as e:
        if "already running" in str(e):
            raise HTTPException(
                status_code=409,
                detail=f"Job '{job}' is already running",
            )
        raise HTTPException(
            status_code=404,
            detail={"error": str(e), "available": sorted(JOB_REGISTRY.keys())},
        )
    return JobResponse(job=job, pid=pid)


# (scheduler lifespan is wired above where `app` is created)


# ── Routes: self-learning ───────────────────────────────
@app.post("/learn", response_model=LearnResponse, tags=["learn"], dependencies=[Depends(verify_bearer)])
def learn_route(req: LearnRequest, background: BackgroundTasks) -> LearnResponse:
    """Submit a session transcript for distillation. Runs in background — returns immediately.

    The pipeline (extract → distill via Jenna → embed → contradiction-check) is fire-and-forget
    so the caller (Claude Code SessionEnd hook, OpenClaw agent, iOS Shortcut) never blocks.
    """
    candidates = learn.extract_candidates(req.transcript)
    background.add_task(_run_learn_pipeline, req.transcript, req.source, req.agent)
    return LearnResponse(candidates=len(candidates))


def _run_learn_pipeline(transcript: str, source: str, agent: str) -> None:
    try:
        result = learn.process_session(transcript, source=source, agent=agent)
        if result.get("errors"):
            _log_failure(f"learn errors: {result['errors']}", route="/learn")
        elif result.get("stored", 0) > 0:
            _metrics_buf.record_learn_success()
    except Exception as e:
        _log_failure(f"learn pipeline crash: {e}", route="/learn")


# ── Routes: memory CRUD ─────────────────────────────────
def _memory_collection_id() -> str | None:
    _ensure_collection(learn.SEMANTIC_COLLECTION)
    return _get_col_id(learn.SEMANTIC_COLLECTION)


def _contradictions_collection_id() -> str | None:
    _ensure_collection(learn.CONTRADICTIONS_COLLECTION)
    return _get_col_id(learn.CONTRADICTIONS_COLLECTION)


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
        col_id = _memory_collection_id()
        if not col_id:
            raise HTTPException(status_code=503, detail="semantic_memory collection unavailable")

        where: dict[str, Any] = {}
        if category:
            where["category"] = category
        if agent:
            where["agent"] = agent

        # ChromaDB GET doesn't support ordering. Fetch up to 500 matching entries,
        # sort by created_at descending (newest first), then paginate in-memory.
        # NOTE: 500-entry fetch cap is a performance trade-off — keeps Chroma
        # response times under ~300ms. Pagination beyond 500 returns stale results.
        fetch_body: dict[str, Any] = {
            "limit": min(limit * 3, 500),
            "include": ["documents", "metadatas"],
        }
        if where:
            fetch_body["where"] = where if len(where) == 1 else {"$and": [{k: v} for k, v in where.items()]}

        _col_base = f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}"
        try:
            res = _chroma_api("POST", f"{_col_base}/get", fetch_body)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"chroma get failed: {e}")

        # Get real total count from ChromaDB (not just len of capped fetch)
        try:
            real_count = _chroma_api("GET", f"{_col_base}/count")
            total = int(real_count) if isinstance(real_count, (int, str)) else 0
        except Exception:
            total = 0

        ids = res.get("ids") or []
        docs = res.get("documents") or []
        metas = res.get("metadatas") or []
        all_entries = [
            MemoryEntry(id=i, content=d or "", metadata=(m or {}))
            for i, d, m in zip(ids, docs, metas)
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


@app.get("/memory/contradictions", response_model=ContradictionListResponse, tags=["memory"], dependencies=[Depends(verify_bearer)])
def list_contradictions(limit: int = 50) -> ContradictionListResponse:
    col_id = _contradictions_collection_id()
    if not col_id:
        return ContradictionListResponse(results=[], total=0)

    _where = {"review_state": "pending"}
    _col_path = f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}"
    try:
        # Total count (IDs only, cheap)
        count_res = _chroma_api("POST", f"{_col_path}/get", {"where": _where, "include": [], "limit": 10000})
        total = len(count_res.get("ids") or [])
        # Paginated fetch with content
        res = _chroma_api("POST", f"{_col_path}/get", {
            "limit": min(max(limit, 1), 200),
            "where": _where,
            "include": ["documents", "metadatas"],
        })
    except Exception:
        return ContradictionListResponse(results=[], total=0)

    ids = res.get("ids") or []
    docs = res.get("documents") or []
    metas = res.get("metadatas") or []
    entries: list[ContradictionEntry] = []
    for i, doc, meta in zip(ids, docs, metas):
        meta = meta or {}
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
        entries.append(ContradictionEntry(
            id=i,
            new_content=new_content,
            old_content=old_content,
            category=meta.get("category", ""),
            distance=float(meta.get("distance", 0)),
            token_overlap=float(meta.get("token_overlap", 0)),
            review_state=meta.get("review_state", "pending"),
            created_at=meta.get("created_at", ""),
            metadata=meta,
        ))
    return ContradictionListResponse(results=entries, total=total)


@app.get("/memory/export", tags=["memory"], dependencies=[Depends(verify_bearer)])
def export_memory() -> list[dict]:
    """Export all semantic_memory entries as a JSON array for backup/migration."""
    col_id = _memory_collection_id()
    if not col_id:
        raise HTTPException(status_code=503, detail="semantic_memory collection unavailable")
    _col_base = f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}"
    page_size = 2000
    all_results: list[dict] = []
    offset = 0
    try:
        while True:
            res = _chroma_api("POST", f"{_col_base}/get", {
                "limit": page_size, "offset": offset,
                "include": ["documents", "metadatas"],
            })
            ids = res.get("ids") or []
            docs = res.get("documents") or []
            metas = res.get("metadatas") or []
            for i, d, m in zip(ids, docs, metas):
                all_results.append({"id": i, "content": d or "", "metadata": m or {}})
            if len(ids) < page_size:
                break
            offset += page_size
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"chroma get failed: {e}")
    return all_results


@app.get("/memory/{mem_id}", response_model=MemoryEntry, tags=["memory"], dependencies=[Depends(verify_bearer)])
def get_memory(mem_id: Annotated[str, PathParam()]) -> MemoryEntry:
    col_id = _memory_collection_id()
    if not col_id:
        raise HTTPException(status_code=503, detail="semantic_memory collection unavailable")
    try:
        res = _chroma_api(
            "POST",
            f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/get",
            {"ids": [mem_id], "include": ["documents", "metadatas"]},
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"chroma get failed: {e}")
    ids = res.get("ids") or []
    if not ids:
        raise HTTPException(status_code=404, detail=f"memory '{mem_id}' not found")
    docs = res.get("documents") or [""]
    metas = res.get("metadatas") or [{}]
    return MemoryEntry(id=ids[0], content=docs[0] or "", metadata=metas[0] or {})


@app.post("/memory", response_model=MemoryEntry, tags=["memory"], dependencies=[Depends(verify_bearer)])
def create_memory(req: MemoryCreateRequest) -> MemoryEntry:
    """Direct memory insert with Phase 1 lifecycle (operations, supersession, temporal, tiers)."""
    col_id = _memory_collection_id()
    if not col_id:
        raise HTTPException(status_code=503, detail="semantic_memory collection unavailable")

    mem_id = f"{learn.SEMANTIC_COLLECTION}:{learn._digest(req.content)}"
    embedding = _get_embedding(req.content[:learn.EMBED_TRUNCATE])
    if not embedding:
        raise HTTPException(status_code=502, detail="embedding failed")

    now_iso = learn._now_iso()

    # Phase 1A: Memory operations classification (Mem0-inspired)
    operation = "ADD"
    supersede_target = None
    try:
        from memory_operations import classify_operation, should_delete_by_content  # noqa: E402
        # Always run classify_operation to find a target (for DELETE/UPDATE/NOOP)
        op, target_id, _diag = classify_operation(
            req.content, embedding, req.confidence, col_id,
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
            _chroma_api(
                "POST",
                f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/delete",
                {"ids": [supersede_target]},
            )
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
        "confidence": str(round(req.confidence, 3)),
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
        # Phase 1E: trust score
        "trust_score": "0.5",
    }

    # Phase 1B: on UPDATE, mark old memory as superseded
    if operation == "UPDATE" and supersede_target:
        try:
            _chroma_api(
                "POST",
                f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/update",
                {
                    "ids": [supersede_target],
                    "metadatas": [{"superseded_by": mem_id, "valid_until": now_iso}],
                },
            )
        except Exception as e:
            print(f"WARNING failed to mark {supersede_target} superseded: {e}")

    try:
        _chroma_api(
            "POST",
            f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/upsert",
            {
                "ids": [mem_id],
                "embeddings": [embedding],
                "documents": [req.content],
                "metadatas": [metadata],
            },
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"chroma upsert failed: {e}")

    _metrics_buf.record_memory_write()
    # Fire hook (Phase 6A)
    try:
        import hooks  # noqa: E402
        hooks.fire("on_memory_stored", mem_id=mem_id, category=req.category, operation=operation)
    except Exception:
        pass

    # Phase 3 atoms-truth-layer mirror + Phase 6 30-word discipline.
    # Best-effort, gated by BRAIN_ATOMS_ENABLED.
    try:
        from atoms_gate import enforce as _atoms_enforce
        from atoms_store import mark_superseded, upsert_atom

        atom_text, atom_status, atom_quality = _atoms_enforce(
            req.content[:2000],
            allow_redistill=False,  # POST /memory is sync — don't block on Jenna here
        )
        upsert_atom(
            text=atom_text,
            chroma_id=mem_id,
            kind=req.category or "fact",
            confidence=req.confidence,
            tier="episodic",
            distilled_by="manual",
            collection_hint="semantic_memory",
            quality_score=atom_quality,
            valid_from=now_iso,
            provenance={
                "agent": req.agent,
                "source": req.source,
                "operation": operation,
                "atoms_gate_status": atom_status,
            },
        )
        if operation == "UPDATE" and supersede_target:
            mark_superseded(supersede_target, mem_id)
    except Exception:
        pass

    response_meta = dict(metadata)
    response_meta["operation"] = operation
    return MemoryEntry(id=mem_id, content=req.content, metadata=response_meta)


class MemoryBatchRequest(BaseModel):
    memories: list[MemoryCreateRequest] = Field(..., min_length=1, max_length=50)


@app.post("/memory/batch", tags=["memory"], dependencies=[Depends(verify_bearer)])
def create_memory_batch(req: MemoryBatchRequest) -> dict:
    """Batch insert memories — 10x faster than single /memory calls.

    Each memory still gets individual classification (ADD/UPDATE/NOOP/DELETE)
    but the final ChromaDB upsert is a single batched call.
    """
    col_id = _memory_collection_id()
    if not col_id:
        raise HTTPException(status_code=503, detail="semantic_memory collection unavailable")

    from memory_operations import classify_operation, should_delete_by_content  # noqa: E402

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
        embedding = _get_embedding(mem_req.content[:learn.EMBED_TRUNCATE])
        if not embedding:
            results.append({"id": mem_id, "operation": "SKIP", "reason": "embedding failed"})
            continue

        now_iso = learn._now_iso()
        operation = "ADD"
        supersede_target = None
        try:
            op, target_id, _diag = classify_operation(mem_req.content, embedding, mem_req.confidence, col_id, category=mem_req.category)
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
            "confidence": str(round(mem_req.confidence, 3)),
            "reason": mem_req.reason,
            "created_at": now_iso,
            "type": "manual",
            "embed_model_version": learn.EMBED_MODEL_VERSION,
            "supersedes": supersede_target or "",
            "superseded_by": "",
            "valid_from": now_iso,
            "valid_until": "",
            "memory_class": "episodic",
            "trust_score": "0.5",
        }

        if operation == "UPDATE" and supersede_target:
            supersede_updates.append((supersede_target, mem_id, now_iso))

        ids_to_upsert.append(mem_id)
        embeddings_to_upsert.append(embedding)
        docs_to_upsert.append(mem_req.content)
        metas_to_upsert.append(metadata)
        operations.append(operation)
        results.append({"id": mem_id, "operation": operation})

    # Apply supersede updates (batched)
    if supersede_updates:
        try:
            _chroma_api(
                "POST",
                f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/update",
                {
                    "ids": [u[0] for u in supersede_updates],
                    "metadatas": [{"superseded_by": u[1], "valid_until": u[2]} for u in supersede_updates],
                },
            )
        except Exception as e:
            print(f"WARNING batch supersede failed: {e}")

    # Apply deletes (batched)
    if deletes_to_apply:
        try:
            _chroma_api(
                "POST",
                f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/delete",
                {"ids": deletes_to_apply},
            )
        except Exception as e:
            print(f"WARNING batch delete failed: {e}")

    # Apply upserts (batched)
    if ids_to_upsert:
        try:
            _chroma_api(
                "POST",
                f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/upsert",
                {
                    "ids": ids_to_upsert,
                    "embeddings": embeddings_to_upsert,
                    "documents": docs_to_upsert,
                    "metadatas": metas_to_upsert,
                },
            )
            for _ in ids_to_upsert:
                _metrics_buf.record_memory_write()
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"batch upsert failed: {e}")

    # Fire hooks for stored memories
    try:
        import hooks  # noqa: E402
        for mem_id, op in zip(ids_to_upsert, operations):
            hooks.fire("on_memory_stored", mem_id=mem_id, category="batch", operation=op)
    except Exception:
        pass

    return {
        "stored": len(ids_to_upsert),
        "superseded": len(supersede_updates),
        "deleted": len(deletes_to_apply),
        "total_requested": len(req.memories),
        "results": results,
    }


@app.patch("/memory/{mem_id}", response_model=MemoryEntry, tags=["memory"], dependencies=[Depends(verify_bearer)])
def patch_memory(mem_id: Annotated[str, PathParam()], req: MemoryPatchRequest) -> MemoryEntry:
    col_id = _memory_collection_id()
    if not col_id:
        raise HTTPException(status_code=503, detail="semantic_memory collection unavailable")

    # Read existing
    existing = get_memory(mem_id)
    new_content = req.content if req.content is not None else existing.content
    new_meta = dict(existing.metadata)
    if req.category is not None:
        new_meta["category"] = req.category
    if req.confidence is not None:
        new_meta["confidence"] = str(round(req.confidence, 3))
    new_meta["updated_at"] = learn._now_iso()

    embedding = _get_embedding(new_content[:learn.EMBED_TRUNCATE]) if req.content is not None else None
    upsert_body: dict[str, Any] = {
        "ids": [mem_id],
        "documents": [new_content],
        "metadatas": [new_meta],
    }
    if embedding:
        upsert_body["embeddings"] = [embedding]

    try:
        _chroma_api(
            "POST",
            f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/upsert",
            upsert_body,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"chroma upsert failed: {e}")
    return MemoryEntry(id=mem_id, content=new_content, metadata=new_meta)


@app.delete("/memory/{mem_id}", tags=["memory"], dependencies=[Depends(verify_bearer)])
def delete_memory(mem_id: Annotated[str, PathParam()]) -> dict:
    col_id = _memory_collection_id()
    if not col_id:
        raise HTTPException(status_code=503, detail="semantic_memory collection unavailable")
    try:
        _chroma_api(
            "POST",
            f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/delete",
            {"ids": [mem_id]},
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"chroma delete failed: {e}")
    return {"status": "deleted", "id": mem_id}



@app.post("/memory/contradictions/{contra_id}/resolve", tags=["memory"], dependencies=[Depends(verify_bearer)])
def resolve_contradiction(
    contra_id: Annotated[str, PathParam()],
    req: ContradictionResolveRequest,
) -> dict:
    contra_col = _contradictions_collection_id()
    if not contra_col:
        raise HTTPException(status_code=503, detail="contradictions collection unavailable")

    # Read the contradiction record
    try:
        res = _chroma_api(
            "POST",
            f"/api/v2/tenants/default_tenant/databases/default_database/collections/{contra_col}/get",
            {"ids": [contra_id], "include": ["metadatas"]},
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"chroma get failed: {e}")

    ids = res.get("ids") or []
    if not ids:
        raise HTTPException(status_code=404, detail=f"contradiction '{contra_id}' not found")
    meta = (res.get("metadatas") or [{}])[0] or {}
    new_id = meta.get("new_id")
    old_id = meta.get("old_id")

    sem_col = _memory_collection_id()
    if sem_col:
        if req.action == "keep_new" and old_id:
            try:
                _chroma_api(
                    "POST",
                    f"/api/v2/tenants/default_tenant/databases/default_database/collections/{sem_col}/delete",
                    {"ids": [old_id]},
                )
            except Exception as e:
                log.warning("contradiction_resolution_error", phase="delete_old", error=str(e))
            # Mark winner as superseding loser
            try:
                _chroma_api("POST", f"/api/v2/tenants/default_tenant/databases/default_database/collections/{sem_col}/update", {
                    "ids": [new_id], "metadatas": [{"supersedes": old_id}],
                })
            except Exception as e:
                log.warning("contradiction_resolution_error", phase="supersede", error=str(e))
        elif req.action == "keep_old" and new_id:
            try:
                _chroma_api(
                    "POST",
                    f"/api/v2/tenants/default_tenant/databases/default_database/collections/{sem_col}/delete",
                    {"ids": [new_id]},
                )
            except Exception as e:
                log.warning("contradiction_resolution_error", phase="delete_new", error=str(e))
        elif req.action == "merge" and old_id and new_id:
            # Combine both entries: keep old ID, merge content
            try:
                old_data = _chroma_api("POST", f"/api/v2/tenants/default_tenant/databases/default_database/collections/{sem_col}/get", {
                    "ids": [old_id, new_id], "include": ["documents", "metadatas", "embeddings"],
                })
                docs = old_data.get("documents", [])
                if len(docs) == 2 and docs[0] and docs[1]:
                    merged = docs[0].strip() + "\n\n" + docs[1].strip()
                    merged = merged[:1000]
                    # Re-embed merged content so vector search stays accurate
                    try:
                        new_emb = _get_embedding(merged, use_cache=False, prefix="passage")
                        _chroma_api("POST", f"/api/v2/tenants/default_tenant/databases/default_database/collections/{sem_col}/update", {
                            "ids": [old_id], "documents": [merged], "embeddings": [new_emb],
                        })
                    except Exception as e:
                        log.warning("contradiction_resolution_error", error=str(e))
                        _chroma_api("POST", f"/api/v2/tenants/default_tenant/databases/default_database/collections/{sem_col}/update", {
                            "ids": [old_id], "documents": [merged],
                        })
                    _chroma_api("POST", f"/api/v2/tenants/default_tenant/databases/default_database/collections/{sem_col}/delete", {
                        "ids": [new_id],
                    })
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
        _chroma_api(
            "POST",
            f"/api/v2/tenants/default_tenant/databases/default_database/collections/{contra_col}/delete",
            {"ids": [contra_id]},
        )
    except Exception as e:
        log.warning("contradiction_resolution_error", error=str(e))

    return {"status": "resolved", "id": contra_id, "action": req.action}


# ── Routes: reasoning + decision ─────────────────────────

def _persist_reasoning_result(title: str, content: str, domain: str, confidence: float) -> None:
    """Karpathy principle: valuable analysis should accumulate, not evaporate into chat history."""
    if confidence < 0.7:
        return
    try:
        import hashlib
        slug = hashlib.md5(title.encode()).hexdigest()[:12]
        note_path = BRAIN_DIR.parent / "knowledge" / "distilled" / "decisions" / f"brain_analysis_{slug}.md"
        if note_path.exists():
            return  # already persisted
        note_path.parent.mkdir(parents=True, exist_ok=True)
        meta = {
            "id": f"dist_brain_analysis_{slug}",
            "type": "distilled", "domain": domain or "decisions",
            "subtype": "brain-analysis", "title": title[:120],
            "status": "active", "confidence": round(confidence, 2),
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "sources": ["brain_reasoning_api"],
        }
        import json as _json
        with note_path.open("w") as f:
            f.write("---json\n")
            f.write(_json.dumps(meta, indent=2, ensure_ascii=False))
            f.write("\n---\n\n")
            f.write(content[:2000])
    except Exception:
        pass  # never let persistence break the API response


@app.post("/brain/decide", response_model=DecideResponse, tags=["decide"],
          dependencies=[Depends(verify_bearer)])
def brain_decide(req: DecideRequest) -> DecideResponse:
    """Agent asks brain for a structured decision recommendation."""
    start = time.time()
    try:
        from brain_core.reasoning import evaluate_decision, DecisionOption
        options = [DecisionOption(label=o.get("label", ""), description=o.get("description", ""))
                   for o in req.options]
        result = evaluate_decision(req.situation, options, req.agent, req.domain)
        evidence = [{"content": h.content[:200], "category": h.category,
                     "confidence": h.confidence, "source": h.source}
                    for h in result.preference_hits[:5]]
        resp = DecideResponse(
            situation=req.situation,
            recommendation=result.recommendation,
            reasoning=result.reasoning,
            confidence=result.confidence,
            evidence=evidence,
            exceptions=result.exceptions,
            model=result.model,
            latency_ms=int((time.time() - start) * 1000),
            cached=result.cached,
            heuristic_fallback=result.heuristic_fallback,
        )
        _persist_reasoning_result(
            f"Decision: {req.situation[:80]} → {result.recommendation}",
            f"## Situation\n{req.situation}\n\n## Recommendation\n{result.recommendation}\n\n## Reasoning\n{result.reasoning}",
            req.domain or "decisions", result.confidence,
        )
        return resp
    except Exception as e:
        _log_failure(str(e)[:500], route="/brain/decide")
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.post("/brain/reason", response_model=ReasonResponse, tags=["decide"],
          dependencies=[Depends(verify_bearer)])
def brain_reason(req: ReasonRequest) -> ReasonResponse:
    """Deeper multi-step reasoning for complex questions."""
    start = time.time()
    try:
        from brain_core.reasoning import reason_deep
        result = reason_deep(req.question, req.context, req.agent, req.domain)
        resp = ReasonResponse(
            question=req.question,
            analysis=getattr(result, "answer", ""),
            reasoning_steps=getattr(result, "reasoning_steps", []),
            confidence=getattr(result, "confidence", 0.0),
            provenance=[vars(p) if hasattr(p, '__dict__') else p for p in getattr(result, "provenance", [])],
            model=getattr(result, "model", "sage"),
            latency_ms=int((time.time() - start) * 1000),
        )
        _persist_reasoning_result(
            f"Analysis: {req.question[:80]}",
            f"## Question\n{req.question}\n\n## Analysis\n{getattr(result, 'answer', '')}",
            req.domain or "analysis", getattr(result, "confidence", 0.0),
        )
        return resp
    except Exception as e:
        _log_failure(str(e)[:500], route="/brain/reason")
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.get("/brain/proactive", tags=["decide"], dependencies=[Depends(verify_bearer)])
def brain_proactive(severity: str | None = None, max_age_hours: int = 24) -> dict:
    """Returns current proactive insights/alerts."""
    try:
        from brain_core.proactive import get_current_insights
        insights = get_current_insights(max_age_hours=max_age_hours, severity=severity)
        return {
            "insights": [vars(i) if hasattr(i, '__dict__') else i for i in insights],
            "total": len(insights),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.post("/brain/proactive/{insight_id}/dismiss", tags=["decide"],
          dependencies=[Depends(verify_bearer)])
def dismiss_proactive(insight_id: str) -> dict:
    """Mark a proactive insight as acknowledged."""
    try:
        from brain_core.proactive import dismiss_insight
        ok = dismiss_insight(insight_id)
        return {"status": "dismissed" if ok else "not_found", "id": insight_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.get("/brain/insights", tags=["decide"], dependencies=[Depends(verify_bearer)])
def brain_insights(days: int = Query(default=7, ge=1, le=30)) -> dict:
    """Return recent daily insights produced by proactive_linker.

    Reads from /Users/chrischo/server/knowledge/distilled/insights/{date}.md.
    Each file has JSON frontmatter (between `---json` / `---` fences) plus a
    markdown body containing one section per insight.
    """
    from datetime import datetime as _dt, timedelta as _td
    # Sibling of DISTILLED_DAILY (knowledge/distilled/daily) — derive from
    # config so this respects KNOWLEDGE_DIR / BRAIN_DIR overrides.
    insights_dir = DISTILLED_DAILY.parent / "insights"
    if not insights_dir.exists():
        return {"days": days, "files": 0, "results": []}

    out: list[dict] = []
    today = _dt.now().date()
    for offset in range(days):
        d = today - _td(days=offset)
        f = insights_dir / f"{d.isoformat()}.md"
        if not f.exists():
            continue
        try:
            text = f.read_text()
        except Exception:
            continue

        # Parse frontmatter: looks like "---json\n{...}\n---\n# body..."
        meta: dict = {}
        body = text
        if text.startswith("---json"):
            try:
                _, rest = text.split("---json\n", 1)
                meta_json, body = rest.split("\n---\n", 1)
                meta = json.loads(meta_json)
            except Exception:
                pass
        elif text.startswith("---\n"):
            try:
                _, rest = text.split("---\n", 1)
                meta_block, body = rest.split("\n---\n", 1)
                meta = json.loads(meta_block) if meta_block.strip().startswith("{") else {}
            except Exception:
                pass

        # Parse body sections — `## N. title\n\ndescription\n` blocks
        sections: list[dict] = []
        current_title: str | None = None
        current_desc_lines: list[str] = []
        for line in body.splitlines():
            if line.startswith("## "):
                if current_title is not None:
                    sections.append({
                        "title": current_title,
                        "description": "\n".join(current_desc_lines).strip()[:600],
                    })
                # Strip leading "N. " ordinal if present
                t = line[3:].strip()
                if t and t[0].isdigit():
                    parts = t.split(". ", 1)
                    if len(parts) == 2:
                        t = parts[1]
                current_title = t
                current_desc_lines = []
            elif current_title is not None:
                current_desc_lines.append(line)
        if current_title is not None:
            sections.append({
                "title": current_title,
                "description": "\n".join(current_desc_lines).strip()[:600],
            })

        out.append({
            "date": d.isoformat(),
            "title": meta.get("title", f"Daily Insights — {d.isoformat()}"),
            "entities": meta.get("entities", []),
            "confidence": meta.get("confidence", 0.0),
            "insights": sections,
        })

    return {"days": days, "files": len(out), "results": out}


# ── Routes: autonomy ──────────────────────────────────────
@app.get("/brain/autopilot", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def get_autopilot() -> dict:
    try:
        from brain_core.autopilot import get_state
        return get_state()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.post("/brain/autopilot", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def set_autopilot(req: AutopilotRequest) -> dict:
    try:
        from brain_core.autopilot import set_state
        state = set_state(req.enabled, req.confidence_threshold, req.updated_by)
        if not req.enabled:
            try:
                from brain_core.task_queue import task_queue
                paused = task_queue.pause_running_tasks()
                state["paused_tasks"] = paused
            except Exception:
                pass
        return state
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.post("/brain/tasks", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def create_task(req: TaskCreateRequest) -> dict:
    try:
        from brain_core.task_queue import task_queue
        agent = req.assigned_agent
        if not agent:
            try:
                from brain_core.reasoning import suggest_delegation
                suggestion = suggest_delegation(req.title + " " + req.description)
                agent = suggestion.get("agent", "jenna")
            except Exception:
                agent = "jenna"
        confidence = req.confidence if req.confidence is not None else 0.5
        return task_queue.create_task(
            title=req.title, description=req.description, assigned_agent=agent,
            priority=req.priority, parent_goal_id=req.parent_goal_id,
            confidence=confidence, created_by="api", metadata=req.metadata)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.get("/brain/tasks", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def list_tasks(status: str | None = None, agent: str | None = None,
               goal: str | None = None, limit: int = 50) -> dict:
    try:
        from brain_core.task_queue import task_queue
        tasks = task_queue.list_tasks(status=status, agent=agent, parent_goal_id=goal, limit=limit)
        return {"tasks": tasks, "total": len(tasks)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.post("/brain/tasks/process", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def process_pending_tasks() -> dict:
    """Manually trigger the autopilot approval sweep. Auto-approves pending tasks above confidence threshold."""
    try:
        from brain_core.autopilot import is_enabled, get_state
        from brain_core.task_queue import task_queue
        state = get_state()
        if not is_enabled():
            return {"approved": [], "autopilot_enabled": False, "message": "autopilot is off"}
        approved, escalated = task_queue.process_pending()
        return {
            "approved": [{"id": t["id"], "title": t["title"], "confidence": t["confidence"]} for t in approved],
            "escalated": [{"id": t["id"], "title": t["title"], "confidence": t["confidence"]} for t in escalated],
            "total_approved": len(approved),
            "total_escalated": len(escalated),
            "autopilot_enabled": True,
            "confidence_threshold": state["confidence_threshold"],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.post("/brain/tasks/dispatch", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def dispatch_ready_tasks() -> dict:
    """Dispatch all ready tasks (approved, deps met) to their assigned OpenClaw agents."""
    try:
        from brain_core.autopilot import is_enabled
        from brain_core.task_queue import task_queue
        if not is_enabled():
            return {"dispatched": [], "autopilot_enabled": False, "message": "autopilot is off"}
        results = task_queue.process_ready()
        return {
            "dispatched": [
                {"id": t.get("id"), "title": t.get("title"), "status": t.get("status"), "agent": t.get("assigned_agent")}
                for t in results
            ],
            "total_dispatched": len(results),
            "autopilot_enabled": True,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.get("/brain/tasks/{task_id}", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def get_task(task_id: str) -> dict:
    try:
        from brain_core.task_queue import task_queue
        task = task_queue.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="task not found")
        return task
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.post("/brain/tasks/{task_id}/approve", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def approve_task(task_id: str) -> dict:
    try:
        from brain_core.task_queue import task_queue
        return task_queue.approve_task(task_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.post("/brain/tasks/{task_id}/start", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def start_task(task_id: str) -> dict:
    try:
        from brain_core.task_queue import task_queue
        return task_queue.start_task(task_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


class CompleteTaskRequest(BaseModel):
    result: str = Field(default="", max_length=10000)


@app.post("/brain/tasks/{task_id}/complete", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def complete_task_route(task_id: str, result: str = "", body: CompleteTaskRequest | None = None) -> dict:
    # Accept result from query param OR JSON body. Sync def so FastAPI offloads
    # the blocking SQLite work to its thread pool instead of stalling the
    # event loop for every concurrent caller.
    if not result and body is not None:
        result = body.result or ""
    try:
        from brain_core.task_queue import task_queue
        task = task_queue.get_task(task_id)
        updated = task_queue.complete_task(task_id, result=result)
        try:
            domain = (task.get("metadata") or {}).get("domain", "general") if task else "general"
            task_queue.record_outcome(
                task_id=task_id, domain=domain,
                brain_recommendation=task.get("confidence_reasoning", "") if task else "",
                actual_action=result[:500], chris_override=False,
            )
        except Exception:
            pass
        return updated
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.post("/brain/tasks/{task_id}/reject", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def reject_task(task_id: str) -> dict:
    try:
        from brain_core.task_queue import task_queue
        task = task_queue.get_task(task_id)
        updated = task_queue.fail_task(task_id, error="rejected by Chris")
        try:
            domain = (task.get("metadata") or {}).get("domain", "general") if task else "general"
            task_queue.record_outcome(
                task_id=task_id, domain=domain,
                brain_recommendation=task.get("confidence_reasoning", "") if task else "",
                actual_action="rejected by Chris",
                chris_override=True, override_reason="manual rejection",
            )
        except Exception:
            pass
        return updated
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.post("/brain/goals", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def create_goal(req: GoalCreateRequest) -> dict:
    try:
        from brain_core.task_queue import task_queue
        goal = task_queue.create_goal(title=req.title, description=req.description)
        if req.auto_decompose:
            try:
                from brain_core.goal_decompose import decompose_goal
                subtasks = decompose_goal(goal["id"])
                goal["subtasks"] = subtasks
            except Exception as exc:
                _log_failure(f"auto-decompose failed for {goal['id']}: {exc}", route="/brain/goals")
                goal["decompose_error"] = str(exc)[:200]
        return goal
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.get("/brain/goals", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def list_goals(status: str | None = None) -> dict:
    try:
        from brain_core.task_queue import task_queue
        goals = task_queue.list_goals(status=status)
        return {"goals": goals, "total": len(goals)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.post("/brain/goals/{goal_id}/complete", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def complete_goal_route(goal_id: str) -> dict:
    try:
        from brain_core.task_queue import task_queue
        return task_queue.complete_goal(goal_id, by="chris")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.get("/brain/goals/{goal_id}", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def get_goal(goal_id: str) -> dict:
    try:
        from brain_core.task_queue import task_queue
        goal = task_queue.get_goal(goal_id)
        if not goal:
            raise HTTPException(status_code=404, detail="goal not found")
        goal["progress"] = task_queue.get_goal_progress(goal_id)
        goal["subtasks"] = task_queue.list_tasks(parent_goal_id=goal_id)
        return goal
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.post("/brain/goals/{goal_id}/decompose", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def decompose_goal_endpoint(goal_id: str) -> dict:
    try:
        from brain_core.task_queue import task_queue
        from brain_core.goal_decompose import decompose_goal
        if not task_queue.get_goal(goal_id):
            raise HTTPException(status_code=404, detail="goal not found")
        tasks = decompose_goal(goal_id)
        return {"goal_id": goal_id, "subtasks_created": len(tasks), "tasks": tasks}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


class AgentMessageRequest(BaseModel):
    from_agent: str = Field(..., max_length=32)
    to_agent: str = Field(..., max_length=32)
    content: str = Field(..., min_length=1, max_length=5000)
    message_type: str = Field(default="info", max_length=32)
    priority: int = Field(default=5, ge=1, le=10)
    parent_task_id: str | None = None


# Deprecated: use POST /brain/messages instead
@app.post("/brain/message", tags=["autonomy"], dependencies=[Depends(verify_bearer)], include_in_schema=False)
def send_message(req: AgentMessageRequest) -> dict:
    try:
        from brain_core.agent_messenger import send_message
        return send_message(req.from_agent, req.to_agent, req.content,
                            req.message_type, req.priority, req.parent_task_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.get("/brain/focus", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def get_focus() -> dict:
    try:
        from brain_core.working_memory import get_working_context
        return get_working_context()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.post("/brain/focus", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def add_focus(req: FocusRequest) -> dict:
    try:
        from brain_core.working_memory import add_focus
        return add_focus(req.content, req.category, req.agent, req.expires_hours)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.delete("/brain/focus/{focus_id}", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def delete_focus(focus_id: str) -> dict:
    try:
        from brain_core.working_memory import remove_focus
        ok = remove_focus(focus_id)
        return {"status": "removed" if ok else "not_found", "id": focus_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


# ── Phase D1: Agent messaging endpoints ──
@app.post("/brain/messages", tags=["coordination"], dependencies=[Depends(verify_bearer)])
def send_agent_message(req: AgentMessageRequest) -> dict:
    """Send a message from one agent to another via the brain."""
    try:
        from brain_core.agent_messenger import send_message
        msg = send_message(
            from_agent=req.from_agent,
            to_agent=req.to_agent,
            content=req.content,
            message_type=req.message_type,
            priority=req.priority,
            parent_task_id=req.parent_task_id,
        )
        return msg
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.get("/brain/messages/{agent}", tags=["coordination"], dependencies=[Depends(verify_bearer)])
def get_agent_messages(
    agent: Annotated[str, PathParam()],
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    """Get pending messages for an agent."""
    try:
        from brain_core.agent_messenger import get_pending_messages
        messages = get_pending_messages(agent, limit=limit)
        return {"agent": agent, "total": len(messages), "messages": messages}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.post("/brain/messages/{msg_id}/ack", tags=["coordination"], dependencies=[Depends(verify_bearer)])
def ack_agent_message(msg_id: Annotated[str, PathParam()]) -> dict:
    """Mark a message as delivered."""
    try:
        from brain_core.agent_messenger import deliver_message
        result = deliver_message(msg_id)
        if not result or (isinstance(result, dict) and result.get("error") == "not_found"):
            raise HTTPException(status_code=404, detail=f"message {msg_id} not found")
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.post("/brain/messages/{agent}/dismiss_all", tags=["coordination"], dependencies=[Depends(verify_bearer)])
def dismiss_all_messages(agent: Annotated[str, PathParam()]) -> dict:
    """Bulk-mark all pending messages for an agent as delivered."""
    try:
        from brain_core.agent_messenger import dismiss_all
        count = dismiss_all(agent)
        return {"agent": agent, "dismissed": count}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


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
        from datetime import datetime as _dt, timezone as _tz
        with _votes_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO contradiction_votes (contradiction_id, voter_agent, vote, confidence, reasoning, voted_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (contra_id, req.voter_agent, req.vote, req.confidence, req.reasoning, _dt.now(_tz.utc).isoformat()),
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
        raise HTTPException(status_code=500, detail=str(e)[:200])


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
        raise HTTPException(status_code=500, detail=str(e)[:200])


# ── Phase D4: Session active agents ──
@app.get("/brain/session/{session_id}/active_agents", tags=["coordination"], dependencies=[Depends(verify_bearer)])
def session_active_agents(session_id: Annotated[str, PathParam()]) -> dict:
    """Show which agents have context in this session and their latest keys."""
    try:
        with _session_conn() as conn:
            rows = conn.execute(
                "SELECT agent, key, value, updated_at FROM session_context "
                "WHERE session_id=? ORDER BY updated_at DESC",
                (session_id,),
            ).fetchall()
        by_agent: dict[str, list] = {}
        for agent, key, value, updated_at in rows:
            by_agent.setdefault(agent, []).append({
                "key": key,
                "value": value[:200],
                "updated_at": updated_at,
            })
        return {
            "session_id": session_id,
            "active_agents": list(by_agent.keys()),
            "agent_count": len(by_agent),
            "contexts": by_agent,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.get("/brain/triggers", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def list_triggers_endpoint() -> dict:
    try:
        from brain_core.action_triggers import list_triggers
        triggers = list_triggers()
        return {"triggers": triggers, "total": len(triggers)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


# ── Phase B1: Trigger CRUD ──────────────────────────────────────────────
class TriggerCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    condition_type: str = Field(..., max_length=50)
    condition_config: dict = Field(default_factory=dict)
    action_template: dict = Field(default_factory=dict)
    enabled: bool = True
    cooldown_seconds: int = Field(default=3600, ge=0, le=86400 * 7)


class TriggerUpdateRequest(BaseModel):
    description: str | None = None
    enabled: bool | None = None
    cooldown_seconds: int | None = Field(default=None, ge=0, le=86400 * 7)
    condition_config: dict | None = None
    action_template: dict | None = None


@app.post("/brain/triggers", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def create_trigger_endpoint(req: TriggerCreateRequest) -> dict:
    try:
        from brain_core.action_triggers import create_trigger

        return create_trigger(
            name=req.name,
            description=req.description,
            condition_type=req.condition_type,
            condition_config=req.condition_config,
            action_template=req.action_template,
            enabled=req.enabled,
            cooldown_seconds=req.cooldown_seconds,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.patch("/brain/triggers/{trigger_id}", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def update_trigger_endpoint(trigger_id: str, req: TriggerUpdateRequest) -> dict:
    try:
        from brain_core.action_triggers import update_trigger

        result = update_trigger(
            trigger_id,
            description=req.description,
            enabled=req.enabled,
            cooldown_seconds=req.cooldown_seconds,
            condition_config=req.condition_config,
            action_template=req.action_template,
        )
        if result is None:
            raise HTTPException(status_code=404, detail="trigger not found")
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.delete("/brain/triggers/{trigger_id}", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def delete_trigger_endpoint(trigger_id: str) -> dict:
    try:
        from brain_core.action_triggers import delete_trigger

        ok = delete_trigger(trigger_id)
        if not ok:
            raise HTTPException(status_code=404, detail="trigger not found")
        return {"status": "deleted", "id": trigger_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


# ── Phase B2: Quiet hours ───────────────────────────────────────────────
class QuietHoursRequest(BaseModel):
    start: str = Field(..., pattern=r"^\d{2}:\d{2}$")
    end: str = Field(..., pattern=r"^\d{2}:\d{2}$")
    tz: str = Field(default="America/Los_Angeles", max_length=64)
    exceptions: list[str] = Field(default_factory=list)


def _quiet_hours_from_config() -> dict:
    """Read quiet hours from brain_config, fall back to default_levels."""
    try:
        import sqlite3

        from brain_core.config import AUTONOMY_DB
        from brain_core.default_levels import QUIET_HOURS

        conn = sqlite3.connect(str(AUTONOMY_DB))
        try:
            rows = conn.execute(
                "SELECT key, value FROM brain_config WHERE key LIKE 'quiet_hours.%'"
            ).fetchall()
        finally:
            conn.close()
        cfg = dict(QUIET_HOURS)
        import json as _json

        for k, v in rows:
            short_key = k[len("quiet_hours.") :]
            if short_key == "exceptions":
                try:
                    cfg["exceptions"] = _json.loads(v)
                except Exception:
                    pass
            else:
                cfg[short_key] = v
        return cfg
    except Exception:
        from brain_core.default_levels import QUIET_HOURS

        return dict(QUIET_HOURS)


@app.get("/brain/quiet-hours", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def get_quiet_hours() -> dict:
    return _quiet_hours_from_config()


@app.post("/brain/quiet-hours", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def set_quiet_hours(req: QuietHoursRequest) -> dict:
    try:
        import json as _json
        import sqlite3
        from datetime import datetime as _dt

        from brain_core.autonomy import invalidate_levels_cache
        from brain_core.config import AUTONOMY_DB

        now_iso = _dt.utcnow().isoformat(timespec="seconds")
        conn = sqlite3.connect(str(AUTONOMY_DB))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS brain_config (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL,
                    updated_at TEXT NOT NULL, updated_by TEXT DEFAULT 'system')"""
            )
            for k, v in (
                ("quiet_hours.start", req.start),
                ("quiet_hours.end", req.end),
                ("quiet_hours.tz", req.tz),
                ("quiet_hours.exceptions", _json.dumps(req.exceptions)),
            ):
                conn.execute(
                    "INSERT INTO brain_config (key, value, updated_at, updated_by) "
                    "VALUES (?, ?, ?, 'api') "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                    "updated_at=excluded.updated_at, updated_by='api'",
                    (k, v, now_iso),
                )
            conn.commit()
        finally:
            conn.close()
        invalidate_levels_cache()
        return {"status": "set", **_quiet_hours_from_config()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


# ── Phase B3: Denylist ──────────────────────────────────────────────────
def _denylist_soft_from_config() -> list[str]:
    try:
        import sqlite3

        from brain_core.config import AUTONOMY_DB

        conn = sqlite3.connect(str(AUTONOMY_DB))
        try:
            rows = conn.execute(
                "SELECT key FROM brain_config WHERE key LIKE 'denylist.%' AND value = '1'"
            ).fetchall()
        finally:
            conn.close()
        return [r[0][len("denylist.") :] for r in rows]
    except Exception:
        return []


@app.get("/brain/denylist", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def get_denylist() -> dict:
    from brain_core.default_levels import DENY_PREFIXES

    return {
        "hardcoded": list(DENY_PREFIXES),
        "soft": _denylist_soft_from_config(),
    }


class DenylistEntryRequest(BaseModel):
    prefix: str = Field(..., min_length=1, max_length=100)


@app.post("/brain/denylist/add", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def add_denylist_entry(req: DenylistEntryRequest) -> dict:
    try:
        import sqlite3
        from datetime import UTC as _UTC
        from datetime import datetime as _dt

        from brain_core.autonomy import invalidate_levels_cache
        from brain_core.config import AUTONOMY_DB

        conn = sqlite3.connect(str(AUTONOMY_DB))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS brain_config (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL,
                    updated_at TEXT NOT NULL, updated_by TEXT DEFAULT 'system')"""
            )
            conn.execute(
                "INSERT INTO brain_config (key, value, updated_at, updated_by) "
                "VALUES (?, '1', ?, 'api') "
                "ON CONFLICT(key) DO UPDATE SET value='1', updated_at=excluded.updated_at",
                (f"denylist.{req.prefix}", _dt.now(_UTC).isoformat(timespec="seconds")),
            )
            conn.commit()
        finally:
            conn.close()
        invalidate_levels_cache()
        return {"status": "added", "prefix": req.prefix}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.post("/brain/denylist/remove", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def remove_denylist_entry(req: DenylistEntryRequest) -> dict:
    try:
        import sqlite3

        from brain_core.autonomy import invalidate_levels_cache
        from brain_core.config import AUTONOMY_DB

        conn = sqlite3.connect(str(AUTONOMY_DB))
        try:
            cur = conn.execute(
                "DELETE FROM brain_config WHERE key = ?", (f"denylist.{req.prefix}",)
            )
            conn.commit()
        finally:
            conn.close()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="prefix not found in soft denylist")
        invalidate_levels_cache()
        return {"status": "removed", "prefix": req.prefix}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


# ── Phase B4: Eval proposals CRUD ────────────────────────────────────────
@app.get("/brain/eval-proposals", tags=["eval"], dependencies=[Depends(verify_bearer)])
def list_eval_proposals(status: str = "candidate", limit: int = 50) -> dict:
    try:
        from brain_core.eval_proposals import list_candidates, stats

        return {
            "items": list_candidates(status=status, limit=limit),
            "stats": stats(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


class EvalProposalCreateRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    expected: str = Field(..., min_length=1, max_length=2000)
    expected_sources: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    source_event: str = Field(default="manual", max_length=64)


@app.post("/brain/eval-proposals", tags=["eval"], dependencies=[Depends(verify_bearer)])
def create_eval_proposal(req: EvalProposalCreateRequest) -> dict:
    try:
        from brain_core.eval_proposals import insert_proposal

        pid = insert_proposal(
            query=req.query,
            expected=req.expected,
            expected_sources=req.expected_sources,
            source_event=req.source_event,
            confidence=req.confidence,
        )
        if not pid:
            raise HTTPException(status_code=500, detail="insert returned no id")
        return {"status": "created", "id": pid}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.post("/brain/eval-proposals/{proposal_id}/approve", tags=["eval"], dependencies=[Depends(verify_bearer)])
def approve_eval_proposal(proposal_id: str) -> dict:
    try:
        from brain_core.eval_proposals import mark_status

        ok = mark_status(proposal_id, "promoted")
        if not ok:
            raise HTTPException(status_code=404, detail="proposal not found")
        return {"status": "promoted", "id": proposal_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.post("/brain/eval-proposals/{proposal_id}/reject", tags=["eval"], dependencies=[Depends(verify_bearer)])
def reject_eval_proposal(proposal_id: str) -> dict:
    try:
        from brain_core.eval_proposals import mark_status

        ok = mark_status(proposal_id, "rejected")
        if not ok:
            raise HTTPException(status_code=404, detail="proposal not found")
        return {"status": "rejected", "id": proposal_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.get("/brain/eval-proposals/stats", tags=["eval"], dependencies=[Depends(verify_bearer)])
def eval_proposal_stats() -> dict:
    try:
        from brain_core.eval_proposals import stats

        return stats()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


# ── Phase B5: Atoms introspection ────────────────────────────────────────
@app.get("/brain/atoms/stats", tags=["atoms"], dependencies=[Depends(verify_bearer)])
def atoms_stats() -> dict:
    try:
        from brain_core.atoms_store import count_atoms

        return count_atoms()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.get("/brain/atoms", tags=["atoms"], dependencies=[Depends(verify_bearer)])
def list_atoms(
    tier: str | None = None,
    kind: str | None = None,
    canonical: int | None = None,
    limit: int = 50,
) -> dict:
    try:
        import sqlite3

        from brain_core.atoms_store import BRAIN_ATOMS_ENABLED, BRAIN_DB

        if not BRAIN_ATOMS_ENABLED:
            return {"items": [], "total": 0, "enabled": False}
        limit = max(1, min(500, limit))
        clauses = []
        params: list[object] = []
        if tier:
            clauses.append("tier = ?")
            params.append(tier)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if canonical is not None:
            clauses.append("canonical = ?")
            params.append(int(canonical))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        conn = sqlite3.connect(str(BRAIN_DB))
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                f"SELECT id, text, kind, tier, canonical, confidence, "
                f"reinforcement_count, interval_days, easiness_factor, "
                f"next_review_at, chroma_id, distilled_by, valid_from, valid_until, "
                f"quality_score, created_at "
                f"FROM atoms{where} ORDER BY created_at DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
        finally:
            conn.close()
        return {"items": [dict(r) for r in rows], "total": len(rows), "enabled": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


# ── Phase E: SLO observability ──────────────────────────────────────────
@app.get("/brain/slos", tags=["observability"], dependencies=[Depends(verify_bearer)])
def get_slos() -> dict:
    """Return current SLO check results without dispatching alerts."""
    try:
        from brain_core.slos import check_all

        results = check_all()
        return {
            "checked": len(results),
            "breached": sum(1 for r in results if r.breached),
            "results": [
                {
                    "name": r.slo.name,
                    "description": r.slo.description,
                    "target": r.slo.target,
                    "actual": r.actual,
                    "delta": r.delta,
                    "breached": r.breached,
                    "severity": r.slo.severity,
                    "unit": r.slo.metric_unit,
                }
                for r in results
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.post("/brain/slos/check", tags=["observability"], dependencies=[Depends(verify_bearer)])
def trigger_slos_check() -> dict:
    """Manually trigger an SLO check + alert dispatch (for testing)."""
    try:
        from brain_core.slos import run

        return run()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.get("/brain/atoms/{atom_id}", tags=["atoms"], dependencies=[Depends(verify_bearer)])
def get_atom_detail(atom_id: str) -> dict:
    try:
        import sqlite3

        from brain_core.atoms_store import BRAIN_ATOMS_ENABLED, BRAIN_DB

        if not BRAIN_ATOMS_ENABLED:
            raise HTTPException(status_code=503, detail="atoms not enabled")
        conn = sqlite3.connect(str(BRAIN_DB))
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM atoms WHERE id = ?", (atom_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="atom not found")
            atom = dict(row)
            prov = conn.execute(
                "SELECT parent_kind, parent_id, child_kind, child_id, relation, confidence "
                "FROM provenance WHERE parent_id = ? OR child_id = ? LIMIT 50",
                (atom_id, atom_id),
            ).fetchall()
            atom["provenance"] = [dict(p) for p in prov]
        finally:
            conn.close()
        return atom
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.get("/brain/trace/{note_id}", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def trace_provenance(note_id: str, max_depth: int = 3) -> dict:
    """Trace relation chains from a canonical note."""
    try:
        from brain_core.provenance import trace
        if max_depth < 0 or max_depth > 10:
            raise HTTPException(status_code=400, detail="max_depth must be 0-10")
        return trace(note_id, max_depth=max_depth)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.post("/brain/ingest", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def brain_ingest(req: BrainIngestRequest) -> dict:
    """Manual ingest: submit text/URL for LLM extraction and integration into knowledge base."""
    try:
        content = req.content
        source_name = req.source

        from brain_core.openclaw_dispatch import dispatch
        prompt = (
            f"Extract key facts, decisions, and insights from this content. "
            f"Write a concise summary as a knowledge note.\n\n"
            f"Source: {source_name}\n"
            f"Content:\n{content[:5000]}\n\n"
            f"Return ONLY a JSON object:\n"
            f'{{"title": "...", "summary": "...", "key_facts": ["..."], "domain": "decisions|infra|projects|chris"}}'
        )
        result = dispatch(agent="sage", message=prompt, thinking="low", timeout=60)
        if not result.ok:
            return {"status": "dispatch_failed", "error": result.error[:200]}

        # Parse and write to raw/inbox
        import json as _json
        try:
            extracted = _json.loads(result.text.strip().strip("`").strip())
        except _json.JSONDecodeError:
            extracted = {"title": source_name, "summary": result.text[:500], "key_facts": [], "domain": "decisions"}

        inbox_dir = BRAIN_DIR.parent / "knowledge" / "raw" / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        import hashlib
        slug = hashlib.md5(content[:200].encode()).hexdigest()[:12]
        record = {
            "id": f"raw_manual_{slug}",
            "type": "raw", "subtype": "manual_ingest",
            "title": extracted.get("title", source_name)[:120],
            "content": extracted.get("summary", ""),
            "key_facts": extracted.get("key_facts", []),
            "domain": extracted.get("domain", "decisions"),
            "source": source_name,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        out_path = inbox_dir / f"manual_{slug}.json"
        out_path.write_text(_json.dumps(record, indent=2, ensure_ascii=False))

        return {"status": "ingested", "id": record["id"], "title": record["title"], "path": str(out_path.relative_to(BRAIN_DIR.parent))}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.post("/brain/index/rebuild", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def rebuild_canonical_index() -> dict:
    """Rebuild the canonical knowledge index (navigable map of all canonical notes)."""
    try:
        knowledge_dir = BRAIN_DIR.parent / "knowledge"
        canonical_dir = knowledge_dir / "canonical"
        if not canonical_dir.exists():
            return {"status": "no canonical dir"}

        import json as _json
        entries = []
        for md_file in sorted(canonical_dir.rglob("*.md")):
            try:
                text = md_file.read_text()
                lines = text.splitlines()
                if not lines or not lines[0].startswith("---"):
                    continue
                end_idx = None
                for i in range(1, len(lines)):
                    if lines[i].strip() == "---":
                        end_idx = i
                        break
                if end_idx is None:
                    continue
                meta = _json.loads("\n".join(lines[1:end_idx]))
                title = meta.get("title", md_file.stem)
                note_id = meta.get("id", "")
                domain = meta.get("domain", "")
                status = meta.get("status", "")
                confidence = meta.get("confidence", 0)
                # Extract first meaningful line of body as summary
                body_lines = [l.strip() for l in lines[end_idx+1:] if l.strip() and not l.startswith("#")]
                summary = body_lines[0][:120] if body_lines else ""
                entries.append({"id": note_id, "title": title, "domain": domain,
                               "status": status, "confidence": confidence, "summary": summary,
                               "path": str(md_file.relative_to(knowledge_dir))})
            except Exception:
                continue

        # Write index.md
        index_lines = [f"# Canonical Knowledge Index\n",
                       f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n",
                       f"Total: {len(entries)} notes\n\n"]
        by_domain: dict[str, list] = {}
        for e in entries:
            by_domain.setdefault(e["domain"] or "other", []).append(e)
        for domain in sorted(by_domain):
            index_lines.append(f"## {domain} ({len(by_domain[domain])})\n")
            for e in sorted(by_domain[domain], key=lambda x: x["title"]):
                index_lines.append(f"- **{e['title']}** (`{e['id']}`) — {e['summary']}\n")
            index_lines.append("\n")

        index_path = canonical_dir / "index.md"
        index_path.write_text("".join(index_lines))

        return {"status": "rebuilt", "total_notes": len(entries), "domains": len(by_domain), "path": str(index_path.relative_to(knowledge_dir))}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


# ── Routes: audit log ──────────────────────────────────
@app.get("/brain/audit", tags=["audit"], dependencies=[Depends(verify_bearer)])
def audit_list(type: str | None = None, since: str | None = None, pending: bool = False, limit: int = 50) -> dict:
    try:
        from audit_log import list_events
        return {"events": list_events(event_type=type, since=since, pending_only=pending, limit=limit)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


# ── Phase 5: L0–L3 autonomy gate ────────────────────────────────────────
@app.get("/brain/autonomy", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def autonomy_list() -> dict:
    """Return the merged level table (defaults overlaid with brain_config overrides)."""
    try:
        from autonomy import list_levels

        return {"levels": list_levels()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.get("/brain/autonomy/{kind:path}", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def autonomy_get(kind: str) -> dict:
    try:
        from autonomy import list_levels

        levels = list_levels()
        return {"kind": kind, "level": levels.get(kind, "L1")}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.post("/brain/autonomy/{kind:path}", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def autonomy_set(kind: str, payload: dict) -> dict:
    """Override a level. payload = {"level": "L2", "updated_by": "chris"}."""
    level = payload.get("level")
    if level not in ("L0", "L1", "L2", "L3"):
        raise HTTPException(status_code=400, detail="level must be L0|L1|L2|L3")
    try:
        from autonomy import set_level

        set_level(kind, level, updated_by=payload.get("updated_by", "api"))
        return {"status": "set", "kind": kind, "level": level}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.get("/brain/policy/preview", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def autonomy_preview(kind: str, now: str | None = None) -> dict:
    """Dry-run the gate for a kind at a specific timestamp (ISO8601). For debugging."""
    try:
        from autonomy import authorize

        when = None
        if now:
            from datetime import datetime as _dt
            from zoneinfo import ZoneInfo as _zi

            when = _dt.fromisoformat(now.replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=_zi("UTC"))
        decision = authorize(kind, now=when)
        return decision.to_dict()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.get("/brain/breakers", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def breakers_list() -> dict:
    try:
        from breakers import list_all

        return {
            "breakers": [
                {
                    "kind": b.kind,
                    "state": b.state,
                    "failures": b.failures,
                    "trip_count": b.trip_count,
                    "reset_after_s": b.reset_after_s,
                    "remaining_cooldown_s": round(b.remaining_cooldown_s, 1),
                    "reason": b.reason,
                    "opened_at": b.opened_at,
                    "last_failure_at": b.last_failure_at,
                    "last_action_at": b.last_action_at,
                }
                for b in list_all()
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.post("/brain/breakers/{kind:path}/reset", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def breakers_reset(kind: str) -> dict:
    try:
        from breakers import reset

        snap = reset(kind)
        return {"status": "reset", "kind": snap.kind, "state": snap.state}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


# ── Phase 4: SM-2 spaced repetition review ──────────────────────────────
@app.get("/brain/review", tags=["atoms"], dependencies=[Depends(verify_bearer)])
def brain_review(limit: int = 20, tier: str | None = None) -> dict:
    """List atoms whose next_review_at has passed and need a quality grade."""
    try:
        from sm2 import review_due

        items = review_due(limit=limit, tier=tier)
        return {"items": items, "count": len(items)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.post("/brain/review/{chroma_id:path}", tags=["atoms"], dependencies=[Depends(verify_bearer)])
def brain_review_grade(chroma_id: str, payload: dict) -> dict:
    """Grade an atom 0..5 (SM-2 quality). Updates SM-2 state + may promote tier."""
    quality = payload.get("quality")
    if quality is None or not isinstance(quality, int) or not 0 <= quality <= 5:
        raise HTTPException(status_code=400, detail="quality must be int 0..5")
    try:
        from sm2 import apply_quality

        result = apply_quality(chroma_id, quality=quality)
        if result is None:
            raise HTTPException(status_code=404, detail="atom not found or atoms disabled")
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.get("/brain/audit/stats", tags=["audit"], dependencies=[Depends(verify_bearer)])
def audit_stats_endpoint() -> dict:
    try:
        from audit_log import stats
        return stats()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.post("/brain/audit/{event_id}/review", tags=["audit"], dependencies=[Depends(verify_bearer)])
def audit_review(event_id: str) -> dict:
    try:
        from audit_log import review_event
        review_event(event_id)
        return {"status": "reviewed", "id": event_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


# ── Routes: fact store ─────────────────────────────────
@app.get("/brain/facts", tags=["facts"], dependencies=[Depends(verify_bearer)])
def facts_query(entity: str | None = None, attribute: str | None = None, limit: int = 50) -> dict:
    try:
        from fact_store import query_facts
        return {"facts": query_facts(entity=entity, attribute=attribute, limit=limit)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


class FactStoreRequest(BaseModel):
    entity: str = Field(..., min_length=1, max_length=200)
    attribute: str = Field(..., min_length=1, max_length=200)
    value: str = Field(..., min_length=1, max_length=2000)
    source: str = ""
    source_type: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    valid_from: str = ""
    valid_to: str = ""


@app.post("/brain/facts", tags=["facts"], dependencies=[Depends(verify_bearer)])
def facts_store(req: FactStoreRequest) -> dict:
    try:
        from fact_store import store_fact
        return store_fact(
            entity=req.entity,
            attribute=req.attribute,
            value=req.value,
            source=req.source,
            source_type=req.source_type,
            confidence=req.confidence,
            valid_from=req.valid_from,
            valid_to=req.valid_to,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.get("/brain/facts/entity/{entity_name}", tags=["facts"], dependencies=[Depends(verify_bearer)])
def facts_by_entity(entity_name: str) -> dict:
    try:
        from fact_store import get_entity_facts
        return {"entity": entity_name, "facts": get_entity_facts(entity_name)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.get("/brain/facts/stats", tags=["facts"], dependencies=[Depends(verify_bearer)])
def facts_stats() -> dict:
    try:
        from fact_store import stats
        return stats()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.get("/brain/graph/stats", tags=["graph"], dependencies=[Depends(verify_bearer)])
def graph_stats_endpoint() -> dict:
    try:
        from brain_core.entity_graph import get_graph_stats
        return get_graph_stats()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.get("/brain/graph/nodes", tags=["graph"], dependencies=[Depends(verify_bearer)])
def graph_nodes_endpoint(limit: int = 200) -> dict:
    """Return entities + relations for 3D graph visualization."""
    try:
        from brain_core.neo4j_client import run_query, is_healthy
        if not is_healthy():
            return {"nodes": [], "links": [], "backend": "unavailable"}
        nodes = run_query(
            "MATCH (e:Entity) RETURN e.id AS id, e.name AS name, "
            "coalesce(e.entity_type, 'concept') AS type, "
            "coalesce(e.mention_count, 1) AS mention_count, "
            "coalesce(e.memory_class, 'ephemeral') AS memory_class "
            "ORDER BY e.mention_count DESC LIMIT $limit",
            {"limit": limit},
        )
        links = run_query(
            "MATCH (s:Entity)-[r:RELATES_TO]->(t:Entity) "
            "RETURN s.id AS source, t.id AS target, "
            "coalesce(r.relationship, 'related_to') AS relationship, "
            "coalesce(r.weight, 0.5) AS weight "
            "ORDER BY r.weight DESC LIMIT $limit",
            {"limit": limit * 3},
        )
        return {"nodes": nodes, "links": links, "backend": "neo4j"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.get("/brain/lessons", tags=["brain"], dependencies=[Depends(verify_bearer)])
def get_lessons(agent: str = "system", limit: int = 20):
    """Query failure lessons for an agent."""
    try:
        import failure_memory
        lessons = failure_memory.get_similar_lessons("", agent_id=agent, limit=limit)
        return {"agent": agent, "total": len(lessons), "lessons": lessons}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/brain/usage", tags=["brain"], dependencies=[Depends(verify_bearer)])
def brain_usage(days: int = Query(default=30, ge=1, le=365)):
    """LLM dispatch usage stats for budget monitoring."""
    try:
        import openclaw_dispatch
        return openclaw_dispatch.get_usage_stats(days=days)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
                q, limit,
                sources=["rag", "canonical"],
                include_history=True,  # include superseded for historical accuracy
                include_obsolete=True,
                as_of=date,
            )
            return {
                "date": date,
                "query": q,
                "total": len(payload.get("results", [])),
                "results": payload.get("results", [])[:limit],
            }
        else:
            # No query — summarize: count memories by class that existed on date
            col_id = _memory_collection_id()
            if not col_id:
                raise HTTPException(status_code=503, detail="semantic_memory unavailable")
            # Fetch all memories, filter by temporal validity
            resp = _chroma_api(
                "POST",
                f"/api/v2/tenants/default_tenant/databases/default_database/collections/{col_id}/get",
                {"limit": 10000, "include": ["metadatas"]},
            )
            ids = resp.get("ids", [])
            metas = resp.get("metadatas", []) or []

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
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/brain/changes", tags=["brain"], dependencies=[Depends(verify_bearer)])
def knowledge_changes(
    since: str = Query(default="7d", description="Start of range (e.g. '7d', 'last week', '2026-04-01')"),
    until: str = Query(default="now", description="End of range"),
) -> dict:
    try:
        import temporal_reasoning
        return temporal_reasoning.knowledge_diff(since, until)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"temporal diff failed: {e}")


@app.get("/brain/evolution", tags=["brain"], dependencies=[Depends(verify_bearer)])
def preference_evolution(
    topic: str = Query(..., min_length=2, max_length=200, description="Topic to trace (e.g. 'frontend framework')"),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    try:
        import temporal_reasoning
        timeline = temporal_reasoning.preference_evolution(topic, limit=limit)
        return {"topic": topic, "timeline": timeline, "count": len(timeline)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"evolution query failed: {e}")


# ── Phase E1: Session context API ──
class SessionContextRequest(BaseModel):
    agent: str = Field(..., max_length=32)
    key: str = Field(..., min_length=1, max_length=100)
    value: str = Field(..., max_length=5000)


@contextmanager
def _session_conn():
    import sqlite3
    db = BRAIN_DIR / "logs" / "autonomy.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS session_context (
                session_id TEXT NOT NULL,
                agent TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (session_id, agent, key)
            )
        """)
        yield conn
    finally:
        conn.close()


@app.get("/brain/session/{session_id}/context", tags=["brain"], dependencies=[Depends(verify_bearer)])
def get_session_context(session_id: Annotated[str, PathParam()], agent: str | None = None) -> dict:
    """Read per-session key/value context for agents."""
    try:
        with _session_conn() as conn:
            if agent:
                rows = conn.execute(
                    "SELECT agent, key, value, updated_at FROM session_context WHERE session_id=? AND agent=?",
                    (session_id, agent),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT agent, key, value, updated_at FROM session_context WHERE session_id=?",
                    (session_id,),
                ).fetchall()
        return {
            "session_id": session_id,
            "total": len(rows),
            "items": [{"agent": r[0], "key": r[1], "value": r[2], "updated_at": r[3]} for r in rows],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/brain/session/{session_id}/context", tags=["brain"], dependencies=[Depends(verify_bearer)])
def set_session_context(session_id: Annotated[str, PathParam()], req: SessionContextRequest) -> dict:
    """Set a per-session key/value for an agent."""
    try:
        from datetime import datetime as _dt2, timezone as _tz2
        with _session_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO session_context (session_id, agent, key, value, updated_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, req.agent, req.key, req.value, _dt2.now(_tz2.utc).isoformat()),
            )
            conn.commit()
        return {"status": "ok", "session_id": session_id, "key": req.key}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Round 9: code intelligence ──
@app.get("/brain/code/find", tags=["brain"], dependencies=[Depends(verify_bearer)])
def code_find(
    q: str = Query(..., min_length=1, max_length=500),
    n: int = Query(default=10, ge=1, le=50),
) -> dict:
    """Search the code collection only — function-level results from indexed repos."""
    try:
        from search import get_collections, vector_search, get_embedding
        cols = get_collections()
        col_id = cols.get("code")
        if not col_id:
            return {"results": [], "error": "code collection not found — run /jobs/code_index_refresh first"}
        emb = get_embedding(q, prefix="query")
        data = vector_search(col_id, emb, n=n)
        ids = (data.get("ids") or [[]])[0]
        docs = (data.get("documents") or [[]])[0]
        metas = (data.get("metadatas") or [[]])[0]
        dists = (data.get("distances") or [[]])[0]
        results = []
        for i, d, m, dist in zip(ids, docs, metas, dists):
            results.append({
                "id": i,
                "score": round(max(0.0, 1 - float(dist)) * 100, 2),
                "file_path": (m or {}).get("file_path", ""),
                "function_name": (m or {}).get("function_name", ""),
                "signature": (m or {}).get("signature", ""),
                "language": (m or {}).get("language", ""),
                "line_start": (m or {}).get("line_start", 0),
                "snippet": (d or "")[:600],
            })
        return {"query": q, "total": len(results), "results": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


# ── Phase E2: TodoWrite sync ──
class TodoItem(BaseModel):
    content: str
    status: str = "pending"
    activeForm: str | None = None


class TodoWriteRequest(BaseModel):
    todos: list[TodoItem]
    session_id: str | None = None


@app.post("/brain/todos", tags=["brain"], dependencies=[Depends(verify_bearer)])
def sync_todos(req: TodoWriteRequest) -> dict:
    """Sync TodoWrite state from Claude Code into brain."""
    try:
        from datetime import datetime as _dt3, timezone as _tz3
        with _session_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS todos (
                    session_id TEXT, idx INTEGER, content TEXT, status TEXT,
                    active_form TEXT, updated_at TEXT,
                    PRIMARY KEY (session_id, idx)
                )
            """)
            now = _dt3.now(_tz3.utc).isoformat()
            session = req.session_id or "default"
            conn.execute("DELETE FROM todos WHERE session_id=?", (session,))
            for i, t in enumerate(req.todos):
                conn.execute(
                    "INSERT INTO todos VALUES (?, ?, ?, ?, ?, ?)",
                    (session, i, t.content, t.status, t.activeForm, now),
                )
            conn.commit()
        return {"status": "ok", "count": len(req.todos), "session": session}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/brain/todos", tags=["brain"], dependencies=[Depends(verify_bearer)])
def get_todos(session_id: str = "default", status: str | None = None) -> dict:
    """Query todos by session, optionally filtered by status."""
    try:
        with _session_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS todos (
                    session_id TEXT, idx INTEGER, content TEXT, status TEXT,
                    active_form TEXT, updated_at TEXT,
                    PRIMARY KEY (session_id, idx)
                )
            """)
            if status:
                rows = conn.execute(
                    "SELECT idx, content, status, active_form, updated_at FROM todos WHERE session_id=? AND status=? ORDER BY idx",
                    (session_id, status),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT idx, content, status, active_form, updated_at FROM todos WHERE session_id=? ORDER BY idx",
                    (session_id,),
                ).fetchall()
        return {
            "session_id": session_id,
            "total": len(rows),
            "todos": [
                {"idx": r[0], "content": r[1], "status": r[2], "activeForm": r[3], "updated_at": r[4]}
                for r in rows
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Phase E4: Unified skill discovery ──
@app.get("/brain/skills", tags=["brain"], dependencies=[Depends(verify_bearer)])
def discover_skills(q: str = "", agent: str | None = None, limit: int = 20) -> dict:
    """Search OpenClaw + Claude Code skills via Neo4j skill graph."""
    try:
        from neo4j_client import run_query
        if q:
            rows = run_query(
                "MATCH (s:Skill) WHERE toLower(s.description) CONTAINS toLower($q) OR toLower(s.name) CONTAINS toLower($q) "
                "RETURN s.name AS name, s.description AS description, s.path AS path, "
                "  coalesce(s.use_count, 0) AS use_count "
                "ORDER BY use_count DESC, s.name ASC LIMIT $limit",
                {"q": q, "limit": limit},
            )
        else:
            rows = run_query(
                "MATCH (s:Skill) RETURN s.name AS name, s.description AS description, s.path AS path, "
                "  coalesce(s.use_count, 0) AS use_count "
                "ORDER BY use_count DESC, s.name ASC LIMIT $limit",
                {"limit": limit},
            )
        return {"query": q, "total": len(rows), "skills": rows}
    except Exception as e:
        return {"query": q, "total": 0, "skills": [], "error": str(e)[:200]}


# ── Phase F1: Search quality dashboard ──
@app.get("/brain/search-quality", tags=["brain"], dependencies=[Depends(verify_bearer)])
def search_quality() -> dict:
    """Rolling search quality metrics for the Brain UI dashboard."""
    try:
        stats = _metrics_buf.search_latency_stats() if hasattr(_metrics_buf, "search_latency_stats") else {}
        feedback_file = BRAIN_DIR / "logs" / "search-feedback.jsonl"
        feedback_stats = {"useful": 0, "total": 0}
        if feedback_file.exists():
            try:
                with feedback_file.open() as f:
                    lines = f.readlines()[-500:]
                    for line in lines:
                        try:
                            d = json.loads(line)
                            feedback_stats["total"] += 1
                            if d.get("useful"):
                                feedback_stats["useful"] += 1
                        except Exception:
                            continue
            except Exception:
                pass
        return {
            "p50": stats.get("p50", 0),
            "p95": stats.get("p95", 0),
            "p99": stats.get("p99", 0),
            "count": stats.get("count", 0),
            "feedback": feedback_stats,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/brain/tools", tags=["mcp"], dependencies=[Depends(verify_bearer)])
def brain_tools() -> dict:
    """MCP-compatible tool discovery — lists all brain capabilities for external AI tools."""
    return {"tools": [
        {"name": "brain_recall", "description": "Search Chris's knowledge base (use brain_recall_v2 instead)", "endpoint": "GET /recall?q={query}&n={limit}", "deprecated": True},
        {"name": "brain_recall_v2", "description": "Search with RRF fusion, reranking, graph, time decay. Primary search endpoint.", "endpoint": "GET /recall/v2?q={query}&n={limit}"},
        {"name": "brain_store", "description": "Store a memory/fact/preference", "endpoint": "POST /memory"},
        {"name": "brain_decide", "description": "Get a preference-grounded decision recommendation", "endpoint": "POST /brain/decide"},
        {"name": "brain_reason", "description": "Deep multi-step reasoning with evidence", "endpoint": "POST /brain/reason"},
        {"name": "brain_ingest", "description": "Manually ingest a document or URL into the knowledge base", "endpoint": "POST /brain/ingest"},
        {"name": "brain_trace", "description": "Trace provenance/relation chains from a canonical note", "endpoint": "GET /brain/trace/{note_id}"},
        {"name": "brain_health", "description": "System health check", "endpoint": "GET /brain/health"},
        {"name": "brain_focus", "description": "Get/set working context", "endpoint": "GET/POST /brain/focus"},
        {"name": "brain_proactive", "description": "Current proactive insights and alerts", "endpoint": "GET /brain/proactive"},
    ]}


@app.get("/brain/accuracy", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def brain_accuracy(domain: str | None = None) -> dict:
    try:
        from brain_core.task_queue import task_queue
        return task_queue.get_domain_accuracy(domain=domain)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.get("/brain/outcomes", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def brain_outcomes(domain: str | None = None, limit: int = 50, offset: int = 0) -> dict:
    try:
        from brain_core.task_queue import task_queue
        outcomes = task_queue.list_outcomes(domain=domain, limit=limit, offset=offset)
        return {"outcomes": outcomes, "total": len(outcomes)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.get("/brain/procedures", tags=["autonomy"], dependencies=[Depends(verify_bearer)])
def list_procedures(
    task_type: str | None = Query(default=None, description="Filter by task type (e.g. 'deploy', 'git_workflow')"),
    source: str | None = Query(default=None, description="Filter by source (extraction, shell, manual)"),
    limit: int = Query(default=10, ge=1, le=50),
) -> dict:
    try:
        from brain_core.task_queue import task_queue
        procedures = task_queue.get_procedures(task_type=task_type, source=source, limit=limit)
        return {"procedures": procedures, "total": len(procedures)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"procedure query failed: {e}")


# ── Routes: observability ────────────────────────────────
@app.get("/brain/health", tags=["liveness"], dependencies=[Depends(verify_bearer)])
def brain_health() -> dict:
    """Composite health check — probes all services, returns overall status."""
    import urllib.request

    alerts: list[str] = []
    services: dict[str, str] = {}

    # Probe ChromaDB
    try:
        urllib.request.urlopen("http://127.0.0.1:8000/api/v2/heartbeat", timeout=3)
        services["chromadb"] = "up"
    except Exception:
        services["chromadb"] = "down"
        alerts.append("ChromaDB unreachable")

    # Probe Ollama
    try:
        urllib.request.urlopen("http://127.0.0.1:11434/", timeout=3)
        services["ollama"] = "up"
    except Exception:
        services["ollama"] = "down"
        alerts.append("Ollama unreachable")

    # MinIO health check removed (Brain v2 production hardening, 2026-04-13):
    # brain has no direct MinIO dependency. ChromaDB backups are handled by the
    # independent `ai.openclaw.chroma-backup` launchd plist via mc/aws-cli, not
    # via the brain process. The previous probe pegged a stale OrbStack IP and
    # caused permanent /brain/health=degraded false positive. Container health
    # is verified by docker-compose healthcheck inside the container itself.

    # Probe Neo4j
    try:
        from brain_core.neo4j_client import is_healthy as _neo4j_ok
        services["neo4j"] = "up" if _neo4j_ok() else "down"
    except Exception:
        services["neo4j"] = "down"

    # Collection counts
    collections: dict[str, int] = {}
    try:
        from brain_core.indexer import chroma_api
        cols = chroma_api("GET", "/api/v2/tenants/default_tenant/databases/default_database/collections")
        for c in cols:
            cnt = chroma_api("GET", f"/api/v2/tenants/default_tenant/databases/default_database/collections/{c['id']}/count")
            collections[c["name"]] = int(cnt) if isinstance(cnt, (int, str)) else -1
    except Exception:
        alerts.append("Cannot read collection counts")

    # Latest eval
    eval_info: dict = {}
    eval_history_path = BRAIN_DIR / "logs" / "eval-history.jsonl"
    if eval_history_path.exists():
        try:
            lines = eval_history_path.read_text().strip().splitlines()
            if lines:
                eval_info = json.loads(lines[-1])
        except Exception:
            pass

    # Scheduler failures
    scheduler_failures: list[dict] = []
    for job in brain_scheduler.list_jobs():
        last = job.get("last_run")
        if last and last.get("error"):
            scheduler_failures.append({"job": job["name"], "error": last["error"]})
    if scheduler_failures:
        alerts.append(f"{len(scheduler_failures)} job(s) failed recently")

    # Determine status
    if services.get("chromadb") == "down" or services.get("ollama") == "down":
        status = "unhealthy"
    elif alerts:
        status = "degraded"
    else:
        status = "healthy"

    return {
        "status": status,
        "uptime_sec": int(time.time() - SERVER_START),
        "collections": collections,
        "total_chunks": sum(collections.values()),
        "services": services,
        "eval": eval_info,
        "alerts": alerts,
        "scheduler_failures": scheduler_failures,
        "search_latency": _metrics_buf.search_latency_stats(),
    }


@app.get("/brain/eval-history", tags=["metrics"], dependencies=[Depends(verify_bearer)])
def brain_eval_history(limit: int = 50) -> list:
    """Return recent eval-history entries as a JSON array."""
    eval_path = BRAIN_DIR / "logs" / "eval-history.jsonl"
    if not eval_path.exists():
        return []
    entries = []
    for line in eval_path.read_text().strip().splitlines():
        try:
            entries.append(json.loads(line))
        except Exception:
            continue
    return entries[-limit:]


# ── Phase A6: schema versions ──
@app.get("/brain/schema-versions", tags=["brain"], dependencies=[Depends(verify_bearer)])
def get_schema_versions() -> dict:
    """Show current schema versions for all components."""
    from brain_core.schema_versions import CURRENT_VERSIONS, get_version
    return {
        "components": {
            component: {
                "current_db": get_version(component),
                "code_expects": target,
                "status": "ok" if get_version(component) == target else "mismatch",
            }
            for component, target in CURRENT_VERSIONS.items()
        }
    }


# ── Phase A1: self-healing dispatcher ──
@app.get("/brain/self-heal/status", tags=["brain"], dependencies=[Depends(verify_bearer)])
def self_heal_status(limit: int = 20) -> dict:
    """Show recent healing actions."""
    from brain_core.self_heal import recent_actions, BRAIN_AUTO_HEAL_ENABLED
    return {
        "enabled": BRAIN_AUTO_HEAL_ENABLED,
        "recent_actions": recent_actions(limit),
    }


class HealSignalRequest(BaseModel):
    source: str
    signal_type: str
    severity: str
    metric: str
    value: float
    baseline: float
    target: str = "default"
    context: dict | None = None


@app.post("/brain/self-heal/signal", tags=["brain"], dependencies=[Depends(verify_bearer)])
def emit_heal_signal(req: HealSignalRequest) -> dict:
    """Manually emit a healing signal (for testing + external triggers)."""
    from brain_core.self_heal import HealingSignal, dispatch
    signal = HealingSignal(**req.model_dump())
    return dispatch(signal)


# ── Routes: admin ───────────────────────────────────────
@app.post("/admin/restart", tags=["admin"], dependencies=[Depends(verify_bearer)])
def admin_restart() -> dict:
    # launchd KeepAlive will restart us
    threading.Thread(target=lambda: (time.sleep(1), os._exit(0)), daemon=True).start()
    return {"status": "restarting"}


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
