"""/brain/cross-repo-recall — find analog edits in other repos."""

from __future__ import annotations

from api_deps import verify_bearer
from fastapi import APIRouter, Depends, Query

router = APIRouter(dependencies=[Depends(verify_bearer)])


@router.get("/brain/cross-repo-recall", tags=["brain"])
def cross_repo_recall(
    query: str = Query(..., min_length=3, max_length=400),
    current_repo: str = Query("", max_length=120),
    limit: int = Query(5, ge=1, le=25),
    window_days: int = Query(60, ge=1, le=365),
) -> dict:
    from brain_core import cross_repo_recall as crr
    from config import BRAIN_DB

    items = crr.find_analogs(
        query=query,
        current_repo=current_repo,
        brain_db_path=BRAIN_DB,
        limit=limit,
        window_days=window_days,
    )
    return {
        "query": query,
        "current_repo": current_repo,
        "window_days": window_days,
        "count": len(items),
        "items": items,
    }
