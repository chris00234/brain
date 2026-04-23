"""/brain/wm/* — session working memory (v3 plan)."""

from __future__ import annotations

from typing import Annotated

from api_deps import verify_bearer
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import Path as PathParam
from pydantic import BaseModel, Field
from rate_limit import limiter

router = APIRouter(dependencies=[Depends(verify_bearer)])


class WorkingMemorySetRequest(BaseModel):
    session_id: str = Field(..., max_length=128)
    agent: str = Field(..., max_length=32)
    key: str = Field(..., max_length=200)
    value: str = Field(..., max_length=10000)
    durable: bool = Field(default=False)


@router.post("/brain/wm", tags=["memory"])
@limiter.limit("120/minute")
def wm_set_route(request: Request, req: WorkingMemorySetRequest) -> dict:
    """Set a session working-memory key. Backed by autonomy.db::session_context."""
    from brain_core import test_gate, working_memory

    if req.durable:
        is_test, reason = test_gate.is_test_context(
            session_id=req.session_id, content=req.value, agent=req.agent
        )
        if is_test:
            raise HTTPException(
                status_code=400,
                detail=f"test_data_blocked (durable): {reason}. Use durable=False for test session writes.",
            )

    return working_memory.wm_set(req.session_id, req.agent, req.key, req.value, durable=req.durable)


@router.get("/brain/wm/{session_id}/{agent}/{key:path}", tags=["memory"])
@limiter.limit("600/minute")
def wm_get_route(
    request: Request,
    session_id: Annotated[str, PathParam()],
    agent: Annotated[str, PathParam()],
    key: Annotated[str, PathParam()],
) -> dict:
    from brain_core import working_memory

    value = working_memory.wm_get(session_id, agent, key)
    if value is None:
        raise HTTPException(status_code=404, detail="wm key not found")
    return {"session_id": session_id, "agent": agent, "key": key, "value": value}


@router.get("/brain/wm/{session_id}/{agent}", tags=["memory"])
@limiter.limit("600/minute")
def wm_list_route(
    request: Request,
    session_id: Annotated[str, PathParam()],
    agent: Annotated[str, PathParam()],
) -> dict:
    from brain_core import working_memory

    return {
        "session_id": session_id,
        "agent": agent,
        "keys": working_memory.wm_list(session_id, agent),
    }


@router.delete("/brain/wm/{session_id}/{agent}/{key:path}", tags=["memory"])
@limiter.limit("120/minute")
def wm_delete_route(
    request: Request,
    session_id: Annotated[str, PathParam()],
    agent: Annotated[str, PathParam()],
    key: Annotated[str, PathParam()],
) -> dict:
    from brain_core import working_memory

    ok = working_memory.wm_delete(session_id, agent, key)
    return {"deleted": ok}


@router.post("/brain/wm/{session_id}/consolidate", tags=["memory"])
@limiter.limit("30/minute")
def wm_consolidate_route(request: Request, session_id: Annotated[str, PathParam()]) -> dict:
    """SessionEnd handler: promote durable:* keys to atoms + delete the rest."""
    from brain_core import working_memory

    promoted = working_memory.wm_consolidate(session_id)
    return {"session_id": session_id, "promoted": promoted}
