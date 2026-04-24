"""/jobs + /jobs/{job}/history + POST /jobs/{job} — fire-and-forget surface."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Annotated, Literal

from api_deps import log, verify_bearer
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as PathParam
from job_registry import JOB_REGISTRY, dispatch_job
from pydantic import BaseModel
from scheduler import brain_scheduler

router = APIRouter(dependencies=[Depends(verify_bearer)])


class JobResponse(BaseModel):
    status: Literal["ok"] = "ok"
    job: str
    pid: int


@router.get("/jobs", tags=["jobs"])
def list_jobs() -> dict:
    """List every registered job with its scheduler state + recent history."""
    return {
        "registry": sorted(JOB_REGISTRY.keys()),
        "scheduler": brain_scheduler.list_jobs(),
        "scheduler_resources": brain_scheduler.resource_status(),
    }


@router.get("/jobs/{job}/history", tags=["jobs"])
def job_history(
    job: Annotated[str, PathParam()],
    full: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> dict:
    if job not in JOB_REGISTRY:
        raise HTTPException(status_code=404, detail=f"unknown job '{job}'")
    if not full:
        return {"job": job, "source": "memory", "history": brain_scheduler.get_history(job)}

    history_db = Path("/Users/chrischo/server/brain/logs/scheduler_history.db")
    if not history_db.exists():
        return {"job": job, "source": "sqlite", "history": []}
    try:
        conn = sqlite3.connect(str(history_db), timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT started_at, finished_at, duration_ms, pid, error, manual "
                "FROM job_history WHERE job_name = ? ORDER BY id DESC LIMIT ?",
                (job, limit),
            ).fetchall()
            items = [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception as e:
        log.warning("job_history full query failed: %s", e)
        items = []
    return {"job": job, "source": "sqlite", "count": len(items), "history": items}


@router.post("/jobs/{job}", response_model=JobResponse, tags=["jobs"])
def trigger_job(job: Annotated[str, PathParam()]) -> JobResponse:
    """Manually trigger a job now. Records in scheduler history."""
    try:
        pid = (
            brain_scheduler.trigger_now(job)
            if getattr(brain_scheduler, "_dispatcher", None)
            else dispatch_job(job)
        )
    except ValueError as e:
        if "already running" in str(e):
            raise HTTPException(status_code=409, detail=f"Job '{job}' is already running") from e
        raise HTTPException(
            status_code=404,
            detail={"error": str(e), "available": sorted(JOB_REGISTRY.keys())},
        ) from e
    return JobResponse(job=job, pid=pid)
