"""Admin ops — audit log listing, canonical staleness sweep, self-eval run.

Each route is a thin wrapper over a brain_core helper; no shared module state
so extraction is clean.
"""

from __future__ import annotations

from api_deps import _safe_http_detail, log, verify_bearer
from fastapi import APIRouter, Depends, HTTPException

router = APIRouter(dependencies=[Depends(verify_bearer)])


@router.get("/brain/audit", tags=["audit"])
def audit_list(
    type: str | None = None,
    since: str | None = None,
    pending: bool = False,
    limit: int = 50,
) -> dict:
    try:
        from audit_log import list_events

        return {"events": list_events(event_type=type, since=since, pending_only=pending, limit=limit)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.get("/brain/audit/stats", tags=["audit"])
def audit_stats_endpoint() -> dict:
    try:
        from audit_log import stats

        return stats()
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.post("/brain/audit/{event_id}/review", tags=["audit"])
def audit_review(event_id: str) -> dict:
    try:
        from audit_log import review_event

        review_event(event_id)
        return {"status": "reviewed", "id": event_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.post("/brain/canonical_staleness/run", tags=["canonical"])
def canonical_staleness_run(dry_run: bool = False, max_files: int = 20000) -> dict:
    """Scan distilled canonical .md files for claims invalidated by current code."""
    try:
        from canonical_staleness import scan_distilled

        return scan_distilled(dry_run=dry_run, max_files=max_files)
    except Exception as exc:
        log.warning("canonical_staleness_run failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=_safe_http_detail("internal", exc, route="/brain/canonical_staleness/run"),
        ) from exc


@router.post("/brain/self_eval/run", tags=["self_eval"])
def self_eval_run(verbose: bool = False) -> dict:
    """Sample recent /recall calls, re-run them, measure top-3 overlap drift."""
    try:
        from self_eval import run_self_eval

        result = run_self_eval()
        return result if verbose else {"summary": result.get("summary", result)}
    except Exception as exc:
        log.warning("self_eval_run failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=_safe_http_detail("internal", exc, route="/brain/self_eval/run"),
        ) from exc
