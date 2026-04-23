"""Autonomy gate + breakers + SM-2 review + claude-queue + knowledge changes/evolution."""

from __future__ import annotations

import re

from api_deps import _safe_http_detail, verify_bearer
from fastapi import APIRouter, Depends, HTTPException, Query

router = APIRouter(dependencies=[Depends(verify_bearer)])

_AUTONOMY_KIND_RE = re.compile(r"^[a-z0-9._-]{1,64}$")


# ── Phase 5: L0-L3 autonomy gate ──────────────────────
@router.get("/brain/autonomy", tags=["autonomy"])
def autonomy_list() -> dict:
    try:
        from autonomy import list_levels

        return {"levels": list_levels()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.get("/brain/autonomy/{kind:path}", tags=["autonomy"])
def autonomy_get(kind: str) -> dict:
    try:
        from autonomy import list_levels

        levels = list_levels()
        return {"kind": kind, "level": levels.get(kind, "L1")}
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.post("/brain/autonomy/{kind:path}", tags=["autonomy"])
def autonomy_set(kind: str, payload: dict) -> dict:
    """Override a level. payload = {"level": "L2", "updated_by": "chris"}."""
    if not _AUTONOMY_KIND_RE.match(kind or ""):
        raise HTTPException(status_code=400, detail="kind must match [a-z0-9._-]{1,64}")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be a JSON object")
    level = payload.get("level")
    if level not in ("L0", "L1", "L2", "L3"):
        raise HTTPException(status_code=400, detail="level must be L0|L1|L2|L3")
    updated_by = str(payload.get("updated_by", "api"))[:64]
    try:
        from autonomy import set_level

        set_level(kind, level, updated_by=updated_by)
        return {"status": "set", "kind": kind, "level": level}
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.get("/brain/policy/preview", tags=["autonomy"])
def autonomy_preview(kind: str, now: str | None = None) -> dict:
    """Dry-run the gate for a kind at a specific timestamp. For debugging."""
    try:
        from autonomy import authorize

        when = None
        if now:
            from datetime import datetime as _dt
            from zoneinfo import ZoneInfo as _zi

            when = _dt.fromisoformat(now.replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=_zi("UTC"))
        decision = authorize(kind, now=when)
        return decision.to_dict()
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.get("/brain/breakers", tags=["autonomy"])
def breakers_list() -> dict:
    try:
        from breakers import list_all

        rows = [
            {
                "kind": b.kind,
                "state": b.state,
                "failures": b.failures,
                "trip_count": b.trip_count,
                "reset_after_s": b.reset_after_s,
                "remaining_cooldown_s": round(b.remaining_cooldown_s, 1),
                "reason": b.reason,
                "opened_at": b.opened_at,
                "last_failure_at": b.last_failure_at,
                "last_action_at": b.last_action_at,
            }
            for b in list_all()
        ]
        return {"items": rows, "total": len(rows), "breakers": rows}
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.post("/brain/breakers/{kind:path}/reset", tags=["autonomy"])
def breakers_reset(kind: str) -> dict:
    try:
        from breakers import reset

        snap = reset(kind)
        return {"status": "reset", "kind": snap.kind, "state": snap.state}
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


# ── Phase 4: SM-2 review ──────────────────────────────
@router.get("/brain/review", tags=["atoms"])
def brain_review(limit: int = 20, tier: str | None = None) -> dict:
    """List atoms whose next_review_at has passed and need a quality grade."""
    try:
        from sm2 import review_due

        items = review_due(limit=limit, tier=tier)
        return {"items": items, "total": len(items), "count": len(items)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


@router.post("/brain/review/{chroma_id:path}", tags=["atoms"])
def brain_review_grade(chroma_id: str, payload: dict) -> dict:
    """Grade an atom 0..5 (SM-2 quality)."""
    quality = payload.get("quality")
    if quality is None or not isinstance(quality, int) or not 0 <= quality <= 5:
        raise HTTPException(status_code=400, detail="quality must be int 0..5")
    try:
        from sm2 import apply_quality

        result = apply_quality(chroma_id, quality=quality)
        if result is None:
            raise HTTPException(status_code=404, detail="atom not found or atoms disabled")
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=_safe_http_detail("internal", e)) from e


# ── Claude-session introspection + in-session queue ───
@router.get("/brain/claude-session", tags=["brain"])
def claude_session_info() -> dict:
    """Current session marker state for observability."""
    from brain_core import claude_session

    return claude_session.session_info()


@router.get("/brain/claude-queue/pending", tags=["brain"])
def claude_queue_pending(limit: int = 10, kinds: str = "") -> dict:
    """Claude drains pending in-session LLM requests."""
    from brain_core import claude_session

    kinds_list = [k.strip() for k in kinds.split(",") if k.strip()] if kinds else None
    return {"items": claude_session.drain_pending(limit=limit, kinds=kinds_list)}


@router.post("/brain/claude-queue/{queue_id}/answer", tags=["brain"])
def claude_queue_answer(queue_id: int, body: dict) -> dict:
    """Claude submits an answer for a queued request."""
    from brain_core import claude_session

    answer = str(body.get("answer", ""))
    meta = body.get("meta") or {}
    if not answer:
        raise HTTPException(status_code=400, detail="empty answer")
    ok = claude_session.answer_item(queue_id, answer, meta=meta)
    return {"ok": ok, "queue_id": queue_id}


@router.get("/brain/claude-queue/{queue_id}", tags=["brain"])
def claude_queue_get(queue_id: int) -> dict:
    """Caller polls for answer status on a queued request."""
    from brain_core import claude_session

    r = claude_session.get_answer(queue_id)
    if not r:
        raise HTTPException(status_code=404, detail="not_found")
    return r


# ── Knowledge changes / evolution ─────────────────────
@router.get("/brain/changes", tags=["brain"])
def knowledge_changes(
    since: str = Query(default="7d"),
    until: str = Query(default="now"),
) -> dict:
    try:
        import temporal_reasoning

        return temporal_reasoning.knowledge_diff(since, until)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"temporal diff failed: {e}") from e


@router.get("/brain/evolution", tags=["brain"])
def preference_evolution(
    topic: str = Query(..., min_length=2, max_length=200),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    try:
        import temporal_reasoning

        timeline = temporal_reasoning.preference_evolution(topic, limit=limit)
        return {"topic": topic, "timeline": timeline, "count": len(timeline)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"evolution query failed: {e}") from e
