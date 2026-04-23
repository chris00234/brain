"""SearXNG-backed web search with brain learning (Phase M6)."""

from __future__ import annotations

from api_deps import _safe_http_detail, verify_bearer
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from rate_limit import limiter

router = APIRouter(dependencies=[Depends(verify_bearer)])


class WebSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    limit: int = Field(default=10, ge=1, le=50)
    agent: str = Field(default="mcp", max_length=50)


@router.post("/web/search", tags=["web"])
@limiter.limit("60/minute")
def web_search(request: Request, req: WebSearchRequest) -> dict:
    """Hit SearXNG and return ranked results with per-domain trust scores."""
    try:
        from brain_core.web_search import searxng_query

        results = searxng_query(req.query, n=req.limit, agent=req.agent)
        return {"items": results, "total": len(results), "query": req.query}
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


class WebSearchOutcomeRequest(BaseModel):
    attempt_id: str = Field(..., min_length=1, max_length=50)
    rank: int = Field(..., ge=1, le=100)
    useful: bool


@router.post("/web/search/outcome", tags=["web"])
@limiter.limit("120/minute")
def web_search_outcome(request: Request, req: WebSearchOutcomeRequest) -> dict:
    """Mark a single search result as useful (True) or wrong (False)."""
    try:
        from brain_core.web_search import mark_result_outcome

        ok = mark_result_outcome(req.attempt_id, req.rank, useful=req.useful)
        if not ok:
            raise HTTPException(status_code=404, detail="result not found")
        return {"status": "recorded", "attempt_id": req.attempt_id, "rank": req.rank}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e
