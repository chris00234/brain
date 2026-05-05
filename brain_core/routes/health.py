"""Composite health + eval-history + schema versions + self-heal + admin."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from api_deps import SERVER_START, _safe_http_detail, verify_bearer
from fastapi import APIRouter, Depends, HTTPException
from metrics_buffer import metrics_buffer as _metrics_buf
from pydantic import BaseModel, Field
from scheduler import brain_scheduler
from vector_store import get_vector_store

from config import BRAIN_DIR

router = APIRouter(dependencies=[Depends(verify_bearer)])

LOCAL_MODEL_POLICY = {
    "llm": "disabled",
    "allowed": ["embeddings", "lightweight_rerankers"],
    "ollama_role": "embedder_only",
}


def _parse_eval_timestamp(row: dict) -> datetime | None:
    ts = row.get("timestamp")
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _latest_jsonl(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        for line in reversed(path.read_text().strip().splitlines()):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    except OSError:
        return {}
    return {}


def _latest_eval_tracks(logs_dir: Path) -> dict[str, dict]:
    track_files = {
        "stable": logs_dir / "eval-history-stable.jsonl",
        "extended": logs_dir / "eval-history-extended.jsonl",
        "legacy": logs_dir / "eval-history.jsonl",
    }
    tracks: dict[str, dict] = {}
    for track, path in track_files.items():
        row = _latest_jsonl(path)
        if row:
            row.setdefault("track", track)
            tracks[track] = row
    return tracks


def _latest_eval_summary(eval_tracks: dict[str, dict]) -> dict:
    latest: tuple[datetime, dict] | None = None
    for row in eval_tracks.values():
        dt = _parse_eval_timestamp(row)
        if dt is None:
            continue
        if latest is None or dt > latest[0]:
            latest = (dt, row)
    if latest:
        return latest[1]
    return eval_tracks.get("stable") or eval_tracks.get("extended") or eval_tracks.get("legacy") or {}


def _eval_age_hours(row: dict) -> float | None:
    dt = _parse_eval_timestamp(row)
    if dt is None:
        return None
    return (datetime.now(UTC) - dt).total_seconds() / 3600


def _recent_slo_remediations(limit: int = 10) -> list[dict]:
    try:
        from slo_remediation import recent_actions

        return recent_actions(limit)
    except Exception:
        return []


# ── /brain/health ─────────────────────────────────────
@router.get("/brain/health", tags=["liveness"])
def brain_health() -> dict:
    """Composite health check — probes all services."""
    alerts: list[str] = []
    services: dict[str, str] = {}

    try:
        store = get_vector_store()
        vector_key = store.name
        services[vector_key] = "up" if store.heartbeat() else "down"
    except Exception:
        vector_key = "vector_store"
        services[vector_key] = "down"
    if services.get(vector_key) == "down":
        alerts.append("Vector store unreachable")

    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/", timeout=3):
            pass
        services["ollama_embedder"] = "up"
    except Exception:
        services["ollama_embedder"] = "down"
        alerts.append("Ollama embedder unreachable")

    try:
        from brain_core.neo4j_client import is_healthy as _neo4j_ok

        services["neo4j"] = "up" if _neo4j_ok() else "down"
    except Exception:
        services["neo4j"] = "down"

    collections: dict[str, int] = {}
    try:
        store = get_vector_store()
        for name in store.list_collections():
            collections[name] = store.count(name)
    except Exception:
        alerts.append("Cannot read collection counts")

    logs_dir = BRAIN_DIR / "logs"
    eval_tracks = _latest_eval_tracks(logs_dir)
    eval_info = _latest_eval_summary(eval_tracks)
    if eval_tracks:
        for track in ("stable", "extended"):
            row = eval_tracks.get(track)
            if not row:
                alerts.append(f"{track.capitalize()} eval history missing")
                continue
            age_hours = _eval_age_hours(row)
            if age_hours is not None and age_hours > 36:
                alerts.append(f"{track.capitalize()} eval stale ({age_hours:.0f}h old)")

    scheduler_failures: list[dict] = []
    for job in brain_scheduler.list_jobs():
        last = job.get("last_run")
        if last and last.get("error"):
            scheduler_failures.append({"job": job["name"], "error": last["error"]})
    if scheduler_failures:
        alerts.append(f"{len(scheduler_failures)} job(s) failed recently")

    scheduler_resources = brain_scheduler.resource_status()
    pending_resource_retries = scheduler_resources.get("pending_retries") or {}
    if pending_resource_retries:
        alerts.append(f"{len(pending_resource_retries)} scheduler job(s) deferred by resource budget")

    if services.get(vector_key) == "down" or services.get("ollama_embedder") == "down":
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
        "local_model_policy": LOCAL_MODEL_POLICY,
        "eval": eval_info,
        "eval_tracks": eval_tracks,
        "alerts": alerts,
        "scheduler_failures": scheduler_failures,
        "scheduler_resources": scheduler_resources,
        "slo_remediation": {"recent": _recent_slo_remediations(10)},
        "search_latency": _metrics_buf.search_latency_stats(),
    }


@router.get("/brain/eval-history", tags=["metrics"])
def brain_eval_history(limit: int = 50, track: str = "all") -> list:
    """Return recent eval-history entries."""
    logs_dir = BRAIN_DIR / "logs"
    track_files = {
        "stable": logs_dir / "eval-history-stable.jsonl",
        "extended": logs_dir / "eval-history-extended.jsonl",
        "legacy": logs_dir / "eval-history.jsonl",
    }

    entries: list = []
    files_to_read = [track_files[track]] if track in track_files else list(track_files.values())
    for path in files_to_read:
        if not path.exists():
            continue
        try:
            for line in path.read_text().strip().splitlines():
                try:
                    row = json.loads(line)
                    if "track" not in row:
                        if path.name == "eval-history-stable.jsonl":
                            row["track"] = "stable"
                        elif path.name == "eval-history-extended.jsonl":
                            row["track"] = "extended"
                        else:
                            row["track"] = "legacy"
                    entries.append(row)
                except Exception:  # noqa: S112 — skip malformed history lines
                    continue
        except Exception:  # noqa: S112 — skip unreadable file
            continue

    entries.sort(key=lambda r: r.get("timestamp", ""))
    return entries[-limit:]


# ── Phase A6: schema versions ─────────────────────────
@router.get("/brain/schema-versions", tags=["brain"])
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


# ── Phase A1: self-heal ───────────────────────────────
@router.get("/brain/self-heal/status", tags=["brain"])
def self_heal_status(limit: int = 20) -> dict:
    """Show recent healing actions."""
    from brain_core.self_heal import BRAIN_AUTO_HEAL_ENABLED, recent_actions

    return {"enabled": BRAIN_AUTO_HEAL_ENABLED, "recent_actions": recent_actions(limit)}


class HealSignalRequest(BaseModel):
    source: str
    signal_type: str
    severity: str
    metric: str
    value: float
    baseline: float
    target: str = "default"
    context: dict | None = None


@router.post("/brain/self-heal/signal", tags=["brain"])
def emit_heal_signal(req: HealSignalRequest) -> dict:
    """Manually emit a healing signal."""
    from brain_core.self_heal import HealingSignal, dispatch

    signal = HealingSignal(**req.model_dump())
    return dispatch(signal)


# ── Admin ─────────────────────────────────────────────
class EmbedAdapterRequest(BaseModel):
    path: str | None = Field(default=None, max_length=512)


@router.post("/admin/embed_adapter", tags=["admin"])
def admin_embed_adapter(req: EmbedAdapterRequest) -> dict:
    """Load or clear a LoRA adapter over the base embedder in-process."""
    try:
        from indexer import set_lora_adapter

        if req.path:
            adapter_root = (BRAIN_DIR / "models" / "adapters").resolve()
            try:
                resolved = Path(req.path).expanduser().resolve(strict=False)
            except Exception as exc:
                raise HTTPException(status_code=400, detail="invalid adapter path") from exc
            if not (
                str(resolved) == str(adapter_root) or str(resolved).startswith(str(adapter_root) + os.sep)
            ):
                raise HTTPException(status_code=400, detail="adapter path outside brain/models/adapters")
            result = set_lora_adapter(str(resolved))
        else:
            result = set_lora_adapter(None)
        # Invalidate recall caches so A/B comparisons don't serve stale
        # pre-adapter responses. Best-effort — failure here doesn't revert
        # the adapter swap.
        try:
            from routes.recall import clear_caches

            clear_caches()
        except Exception:  # noqa: S110 — best-effort cache invalidation
            pass
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.post("/admin/restart", tags=["admin"])
def admin_restart() -> dict:
    """Request a launchd restart. exit(1) so KeepAlive sees it as a crash and restarts."""
    threading.Thread(target=lambda: (time.sleep(1), os._exit(1)), daemon=True).start()
    return {"status": "restarting"}
