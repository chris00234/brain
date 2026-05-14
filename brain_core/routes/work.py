"""/brain/work-status — running / failed / deferred jobs at a glance."""

from __future__ import annotations

from api_deps import verify_bearer
from fastapi import APIRouter, Depends, Query

router = APIRouter(dependencies=[Depends(verify_bearer)])


@router.get("/brain/work-status", tags=["brain"])
def work_status(window_hours: int = Query(24, ge=1, le=168)) -> dict:
    from work_status import compute_status

    return compute_status(window_hours=window_hours)
