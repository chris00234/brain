"""/brain/speak/* — brain's outbound voice (digest + urgent + drives)."""

from __future__ import annotations

from typing import Annotated

from api_deps import _safe_http_detail, log, verify_bearer
from fastapi import APIRouter, Depends, HTTPException
from fastapi import Path as PathParam

router = APIRouter(dependencies=[Depends(verify_bearer)])


@router.post("/brain/speak/run", tags=["speak"])
def speak_run(dry_run: bool = False, bypass_dedup: bool = False) -> dict:
    """Run all drives, compose the digest, send via Telegram if non-empty."""
    try:
        from speak import run_digest

        return run_digest(dry_run=dry_run, bypass_dedup=bypass_dedup)
    except Exception as exc:
        log.warning("speak_run failed: %s", exc)
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", exc)) from exc


@router.get("/brain/speak/history", tags=["speak"])
def speak_history(limit: int = 20) -> dict:
    try:
        from speak import recent_history

        return {"items": recent_history(limit=limit)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", exc)) from exc


@router.post("/brain/speak/ack/{entry_id}", tags=["speak"])
def speak_ack(entry_id: Annotated[str, PathParam()], verdict: str) -> dict:
    """Record Chris's feedback on a digest entry: useful / noise / ignore."""
    try:
        from speak import ack as _ack

        ok = _ack(entry_id, verdict)
        if not ok:
            raise HTTPException(status_code=404, detail="entry not found or bad verdict")
        return {"ok": True, "entry_id": entry_id, "verdict": verdict}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", exc)) from exc


@router.post("/brain/speak/urgent", tags=["speak"])
def speak_urgent() -> dict:
    """Fire the urgent doorbell path on demand."""
    try:
        from speak import urgent_scan

        return urgent_scan()
    except Exception as exc:
        log.warning("urgent scan failed: %s", exc)
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", exc)) from exc


@router.get("/brain/speak/drives", tags=["speak"])
def speak_drives() -> dict:
    """Dry-inspect every drive's current observations. Doesn't send."""
    try:
        from speak import collect_observations

        obs = collect_observations()
        return {
            "count": len(obs),
            "observations": [
                {
                    "drive": o.drive,
                    "category": o.category,
                    "severity": o.severity,
                    "message": o.message,
                    "dedup_key": o.dedup_key,
                }
                for o in obs
            ],
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", exc)) from exc
