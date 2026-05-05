"""/metrics, /metrics/prom, /collections — observability snapshots."""

from __future__ import annotations

import os
import time
from typing import Any

from api_deps import SERVER_START, verify_bearer
from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from metrics_buffer import metrics_buffer as _metrics_buf
from profile_cache import profile_cache
from pydantic import BaseModel, Field
from scheduler import brain_scheduler
from vector_store import get_vector_store

router = APIRouter(dependencies=[Depends(verify_bearer)])


class MetricsResponse(BaseModel):
    collection_counts: dict[str, int]
    total_chunks: int
    uptime_sec: int
    profile_loaded: bool
    routes: dict[str, Any] = Field(default_factory=dict)
    phase_latency: dict[str, Any] = Field(default_factory=dict)
    jobs: dict[str, Any] = Field(default_factory=dict)
    dispatch: dict[str, Any] = Field(default_factory=dict)
    memory_writes_1h: int = 0
    scheduler_next_runs: dict[str, str] = Field(default_factory=dict)
    contradiction_queue_depth: int = 0
    last_learn_success_at: str = ""
    last_backup_at: str = ""
    last_backup_ok: bool = True
    embed_cache: dict[str, Any] = Field(default_factory=dict)
    ce_cache: dict[str, Any] = Field(default_factory=dict)
    hook_adoption: dict[str, Any] = Field(default_factory=dict)
    slo_remediation: dict[str, Any] = Field(default_factory=dict)


def _recent_slo_remediations(limit: int = 10) -> list[dict]:
    try:
        from slo_remediation import recent_actions

        return recent_actions(limit)
    except Exception:
        return []


def _get_collection_counts() -> dict[str, int]:
    """Per-collection row counts via the VectorStore abstraction."""
    try:
        store = get_vector_store()
        names = store.list_collections()
    except Exception as e:
        return {"_error": str(e)[:200]}
    counts: dict[str, int] = {}
    for name in names:
        try:
            counts[name] = store.count(name)
        except Exception:
            counts[name] = -1
    return counts


@router.get("/metrics", response_model=MetricsResponse, tags=["metrics"])
def metrics() -> MetricsResponse:
    counts = _get_collection_counts()
    total = sum(c for c in counts.values() if isinstance(c, int) and c >= 0)
    buf = _metrics_buf.snapshot()

    next_runs: dict[str, str] = {}
    try:
        for j in brain_scheduler.list_jobs():
            if j.get("next_run"):
                next_runs[j["name"]] = j["next_run"]
    except Exception:  # noqa: S110 — scheduler introspection is best-effort
        pass

    contradiction_depth = counts.get("semantic_contradictions", 0)
    if not isinstance(contradiction_depth, int) or contradiction_depth < 0:
        contradiction_depth = 0

    try:
        from embed_cache import cache_stats as _embed_stats

        embed_cache = _embed_stats()
    except Exception:
        embed_cache = {}

    if os.getenv("BRAIN_RERANKER_MODE", "inprocess").strip().lower() == "worker":
        ce_cache = {"mode": "worker", "model_loaded_in_api": False}
    else:
        try:
            from brain_core.cross_encoder_model import cache_stats as _ce_stats

            ce_cache = _ce_stats()
        except Exception:
            ce_cache = {}

    slo_recent = _recent_slo_remediations(10)

    return MetricsResponse(
        collection_counts=counts,
        total_chunks=total,
        uptime_sec=int(time.time() - SERVER_START),
        profile_loaded=profile_cache.get() is not None,
        routes=buf["routes"],
        phase_latency=buf.get("phase_latency", {}),
        jobs=buf["jobs"],
        dispatch=buf["dispatch"],
        memory_writes_1h=buf["memory_writes_1h"],
        scheduler_next_runs=next_runs,
        contradiction_queue_depth=contradiction_depth,
        last_learn_success_at=buf.get("last_learn_success_at", ""),
        last_backup_at=buf.get("last_backup_at", ""),
        last_backup_ok=buf.get("last_backup_ok", True),
        embed_cache=embed_cache,
        ce_cache=ce_cache,
        hook_adoption=buf.get("hook_adoption", {}),
        slo_remediation={
            "recent_count": len(slo_recent),
            "latest": slo_recent[-1] if slo_recent else {},
            "recent": slo_recent,
        },
    )


@router.get("/metrics/prom", response_class=PlainTextResponse, tags=["metrics"])
def metrics_prom() -> str:
    """Prometheus exposition format — latency percentiles + counters as gauges."""
    buf = _metrics_buf.snapshot()
    lines: list[str] = []

    def _sanitize(label: str) -> str:
        return label.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")

    lines.append("# HELP brain_route_latency_ms Route latency percentiles")
    lines.append("# TYPE brain_route_latency_ms gauge")
    for path, stats in (buf.get("routes") or {}).items():
        p = _sanitize(path)
        for quantile in ("p50_ms", "p95_ms", "p99_ms"):
            v = stats.get(quantile, 0) or 0
            lines.append(f'brain_route_latency_ms{{path="{p}",quantile="{quantile[:-3]}"}} {float(v)}')
        lines.append(f'brain_route_count{{path="{p}"}} {int(stats.get("count", 0) or 0)}')
        lines.append(f'brain_route_errors{{path="{p}"}} {int(stats.get("errors", 0) or 0)}')

    lines.append("# HELP brain_phase_latency_ms Phase-level latency percentiles")
    lines.append("# TYPE brain_phase_latency_ms gauge")
    for phase, stats in (buf.get("phase_latency") or {}).items():
        p = _sanitize(phase)
        for quantile in ("p50_ms", "p95_ms", "p99_ms"):
            v = stats.get(quantile, 0) or 0
            lines.append(f'brain_phase_latency_ms{{phase="{p}",quantile="{quantile[:-3]}"}} {float(v)}')

    lines.append("# HELP brain_memory_writes_1h Count of memory writes in last hour")
    lines.append("# TYPE brain_memory_writes_1h gauge")
    lines.append(f'brain_memory_writes_1h {int(buf.get("memory_writes_1h", 0) or 0)}')

    dispatch = buf.get("dispatch") or {}
    lines.append("# HELP brain_dispatch Dispatch totals")
    lines.append("# TYPE brain_dispatch counter")
    for key in ("attempts", "successes", "failures", "rate_limited", "auth_failed"):
        lines.append(f'brain_dispatch{{outcome="{key}"}} {int(dispatch.get(key, 0) or 0)}')

    slo_recent = _recent_slo_remediations(100)
    lines.append("# HELP brain_slo_remediation_recent_total Recent SLO remediation records visible on disk")
    lines.append("# TYPE brain_slo_remediation_recent_total gauge")
    lines.append(f"brain_slo_remediation_recent_total {len(slo_recent)}")
    latest_status = str((slo_recent[-1] if slo_recent else {}).get("status") or "none")
    lines.append("# HELP brain_slo_remediation_latest_status Latest SLO remediation status as labeled gauge")
    lines.append("# TYPE brain_slo_remediation_latest_status gauge")
    lines.append(
        f'brain_slo_remediation_latest_status{{status="{_sanitize(latest_status)}"}} {1 if slo_recent else 0}'
    )

    lines.append(f"brain_uptime_seconds {int(time.time() - SERVER_START)}")
    return "\n".join(lines) + "\n"


@router.get("/collections", tags=["metrics"])
def collections_endpoint() -> dict[str, int]:
    return _get_collection_counts()
