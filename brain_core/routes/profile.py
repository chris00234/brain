"""Profile routes — cached identity + state markdown snapshots."""

from __future__ import annotations

from typing import Annotated

from api_deps import verify_bearer
from fastapi import APIRouter, Depends, HTTPException
from fastapi import Path as PathParam
from fastapi.responses import PlainTextResponse
from profile_cache import profile_cache

router = APIRouter(dependencies=[Depends(verify_bearer)])


@router.get("/profile", response_class=PlainTextResponse, tags=["profile"])
def profile() -> str:
    content = profile_cache.get()
    if content is None:
        raise HTTPException(status_code=404, detail="profile not found")
    return content


@router.get("/profile/section/{name}", response_class=PlainTextResponse, tags=["profile"])
def profile_section(name: Annotated[str, PathParam()]) -> str:
    content = profile_cache.section(name)
    if content is None:
        raise HTTPException(status_code=404, detail=f"section '{name}' not found")
    return content
