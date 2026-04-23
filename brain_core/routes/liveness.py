"""Liveness probe — no auth required."""

from __future__ import annotations

import time

from api_deps import SERVER_START, HealthResponse
from fastapi import APIRouter

router = APIRouter(tags=["liveness"])


@router.get("/healthz", response_model=HealthResponse)
def healthz() -> HealthResponse:
    return HealthResponse(uptime_sec=int(time.time() - SERVER_START))
