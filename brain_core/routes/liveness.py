"""Liveness probes — unauthenticated /healthz + /agent/heartbeat."""

from __future__ import annotations

import os
import time

from api_deps import SERVER_START, HealthResponse
from fastapi import APIRouter

router = APIRouter(tags=["liveness"])


@router.get("/healthz", response_model=HealthResponse)
def healthz() -> HealthResponse:
    return HealthResponse(uptime_sec=int(time.time() - SERVER_START))


@router.get("/agent/heartbeat")
def agent_heartbeat() -> dict:
    """Ultra-cheap unauthenticated heartbeat agents can poll.

    Returns uptime + a compact feature-flag summary so agents can detect
    server capabilities before issuing requests (avoids blind 400/404s).
    Does NOT leak any sensitive state — safe for any caller.
    """
    try:
        from brain_core import config as _cfg

        flags = {
            "atoms_read": getattr(_cfg, "BRAIN_ATOMS_READ", False),
            "self_rag": os.environ.get("BRAIN_SELF_RAG_ENABLED", "false").lower()
            in ("1", "true", "yes", "on"),
            "autopilot_killed": os.environ.get("BRAIN_AUTOPILOT_DISABLED", "").strip().lower()
            in ("1", "true", "yes", "on"),
        }
    except Exception:
        flags = {}
    return {
        "status": "ok",
        "uptime_sec": int(time.time() - SERVER_START),
        "features": flags,
    }
