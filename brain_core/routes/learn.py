"""/learn — submit session transcripts for background distillation."""

from __future__ import annotations

from typing import Literal

import learn as _learn
from api_deps import _log_failure, verify_bearer
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from metrics_buffer import metrics_buffer as _metrics_buf
from pydantic import BaseModel, Field
from rate_limit import limiter

router = APIRouter(dependencies=[Depends(verify_bearer)])


class LearnRequest(BaseModel):
    transcript: str = Field(..., min_length=10, max_length=50_000)
    source: str = Field(default="session", max_length=64)
    agent: str = Field(default="claude", max_length=32)


class LearnResponse(BaseModel):
    status: Literal["queued", "ok"] = "queued"
    candidates: int = 0
    message: str = "processing in background"


def _run_learn_pipeline(transcript: str, source: str, agent: str) -> None:
    try:
        result = _learn.process_session(transcript, source=source, agent=agent)
        if result.get("errors"):
            _log_failure(f"learn errors: {result['errors']}", route="/learn")
        elif result.get("stored", 0) > 0:
            _metrics_buf.record_learn_success()
    except Exception as e:
        _log_failure(f"learn pipeline crash: {e}", route="/learn")


@router.post("/learn", response_model=LearnResponse, tags=["learn"])
@limiter.limit("10/minute")
def learn_route(request: Request, req: LearnRequest, background: BackgroundTasks) -> LearnResponse:
    """Submit a session transcript for distillation. Fire-and-forget."""
    try:
        from brain_core import test_gate

        is_test, reason = test_gate.is_test_context(
            source=req.source, content=req.transcript, agent=req.agent
        )
        if is_test:
            raise HTTPException(status_code=400, detail=f"test_data_blocked:{reason}")
    except HTTPException:
        raise
    except Exception:  # noqa: S110 — test_gate import failure must not block the ingest path
        pass
    candidates = _learn.extract_candidates(req.transcript)
    background.add_task(_run_learn_pipeline, req.transcript, req.source, req.agent)
    return LearnResponse(candidates=len(candidates))
