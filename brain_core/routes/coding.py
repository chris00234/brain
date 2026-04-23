"""Coding-events outcome tracking (read-only)."""

from __future__ import annotations

from api_deps import _safe_http_detail, log, verify_bearer
from fastapi import APIRouter, Depends, HTTPException

router = APIRouter(dependencies=[Depends(verify_bearer)])


@router.get("/brain/coding_events", tags=["coding"])
def get_coding_events(file_path: str | None = None, limit: int = 20) -> dict:
    """Return recent coding_events for a file (or aggregate stats if no file)."""
    try:
        from coding_events import get_outcomes_for_file, outcome_stats

        if file_path:
            events = get_outcomes_for_file(file_path, limit=limit)
            return {"file_path": file_path, "events": events, "count": len(events)}
        return {"stats_24h": outcome_stats(within_hours=24)}
    except Exception as exc:
        log.warning("coding_events read failed: %s", exc)
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", exc)) from exc
