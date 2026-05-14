"""/brain/diff — belief diff over a configurable window.

Surfaces what changed in Brain's understanding without forcing the
caller to correlate atoms + decision_ledger by hand.
"""

from __future__ import annotations

from api_deps import verify_bearer
from fastapi import APIRouter, Depends, Query

router = APIRouter(dependencies=[Depends(verify_bearer)])


@router.get("/brain/diff", tags=["brain"])
def brain_diff(
    since_days: int = Query(7, ge=1, le=90),
    limit: int = Query(50, ge=1, le=500),
) -> dict:
    from brain_core import belief_diff

    return belief_diff.compute_diff(since_days=since_days, limit=limit)
