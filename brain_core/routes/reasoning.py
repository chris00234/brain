"""Multi-hop reasoning + boot-context endpoints."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Annotated

import boot_context
from api_deps import _safe_http_detail, get_request_id, verify_bearer
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import Path as PathParam
from fastapi.responses import StreamingResponse
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


# 2026-05-20 W2.2: SSE streaming for multi-hop reasoning so client agents see
# init/hop/plan/search/synthesize events as they happen instead of waiting
# 10-60s for the full reply. Backed by reasoning_loop.iter_reasoning. The
# non-streaming /brain/reason/multihop above stays for callers that only need
# the final synthesis.
@router.post("/brain/reason/multihop/stream", tags=["recall"])
@limiter.limit("10/minute")
def brain_reason_multihop_stream(request: Request, req: MultiHopReasonRequest) -> StreamingResponse:
    """SSE stream of multi-hop reasoning events.

    Events emitted (all as ``event: <name>\\ndata: <json>\\n\\n``):
      - ``init``       — thread_id + question registered
      - ``hop_start``  — entering hop N
      - ``plan``       — Jenna decided next_action=search|synthesize
      - ``search``     — retrieved evidence for the hop's query
      - ``synthesize`` — Jenna produced an answer (final per the hop)
      - ``final``      — full result payload (answer + confidence + citations)
      - ``end``        — terminator with latency_ms
      - ``plan_failed`` / ``plan_parse_failed`` / ``synth_failed`` — failure variants
    """
    rid = get_request_id() or ""

    def _gen() -> Iterator[bytes]:
        t_start = time.time()
        import reasoning_loop

        try:
            for kind, payload in reasoning_loop.iter_reasoning(req.question, max_hops=req.max_hops):
                payload = dict(payload)
                payload.setdefault("rid", rid)
                if kind == "end":
                    payload.setdefault("latency_ms", int((time.time() - t_start) * 1000))
                line = f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                yield line.encode("utf-8")
                if kind == "end":
                    return
        except Exception as exc:
            err = {"error": str(exc)[:200], "rid": rid, "latency_ms": int((time.time() - t_start) * 1000)}
            yield f"event: end\ndata: {json.dumps(err, ensure_ascii=False)}\n\n".encode()

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(_gen(), media_type="text/event-stream", headers=headers)


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
