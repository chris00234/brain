"""Multi-hop reasoning + boot-context endpoints."""

from __future__ import annotations

from typing import Annotated

import boot_context
from api_deps import _safe_http_detail, verify_bearer
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import Path as PathParam
from pydantic import BaseModel, Field
from rate_limit import limiter

router = APIRouter(dependencies=[Depends(verify_bearer)])


class MultiHopReasonRequest(BaseModel):
    question: str = Field(..., min_length=5, max_length=1000)
    max_hops: int = Field(default=5, ge=1, le=5)


@router.post("/brain/reason/multihop", tags=["recall"])
@limiter.limit("10/minute")
def brain_reason_multihop(request: Request, req: MultiHopReasonRequest) -> dict:
    """Multi-hop reasoning with LangGraph-style checkpoints."""
    try:
        import reasoning_loop

        return reasoning_loop.run_reasoning(req.question, max_hops=req.max_hops)
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("reasoning", e)) from e


@router.post("/brain/reason/multihop/{thread_id}/resume", tags=["recall"])
@limiter.limit("10/minute")
def brain_reason_multihop_resume(request: Request, thread_id: Annotated[str, PathParam()]) -> dict:
    """Resume a reasoning thread from last checkpoint."""
    try:
        import reasoning_loop

        return reasoning_loop.resume_reasoning(thread_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=_safe_http_detail("internal", e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("resume", e)) from e


@router.get("/boot-context/{agent}", tags=["recall"])
def boot_ctx(agent: Annotated[str, PathParam()], n: int = 3) -> dict:
    sections = boot_context.build_boot_context(agent, n)
    return {"agent": agent, "sections": sections}


@router.post("/boot-context/flush", tags=["recall"])
def boot_ctx_flush() -> dict:
    boot_context.flush_cache()
    return {"status": "ok", "message": "boot context cache flushed"}
