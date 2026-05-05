"""Valence, attention, predictive, usage — small introspection endpoints."""

from __future__ import annotations

from api_deps import verify_bearer
from fastapi import APIRouter, Depends, Query

router = APIRouter(dependencies=[Depends(verify_bearer)])


# ── Emotional valence layer (biological: amygdala, 2026-04-17) ─────────
@router.post("/brain/valence/{atom_id}", tags=["brain"])
def valence_record(atom_id: str, body: dict) -> dict:
    """Record a valence event for an atom. delta in [-1.0, +1.0]."""
    from brain_core import valence as _val

    delta = float(body.get("delta", 0.0))
    reason = str(body.get("reason", ""))
    source = str(body.get("source", "api"))
    return _val.record_valence(atom_id, delta, reason=reason, source=source)


@router.get("/brain/valence/{atom_id}", tags=["brain"])
def valence_get(atom_id: str) -> dict:
    from brain_core import valence as _val

    return {"atom_id": atom_id, "valence": _val.get_valence(atom_id)}


@router.get("/brain/valence/top/list", tags=["brain"])
def valence_top(direction: str = "both", limit: int = 20) -> dict:
    """Top-valence atoms for observability. direction: positive | negative | both."""
    from brain_core import valence as _val

    return {"items": _val.top_valence(limit=limit, direction=direction)}


@router.get("/brain/valence", tags=["brain"])
def valence_stats() -> dict:
    from brain_core import valence as _val

    return _val.stats()


# ── Attention priority queue (biological: thalamus, 2026-04-17) ────────
@router.get("/brain/attention", tags=["brain"])
def attention_top(limit: int = 1) -> dict:
    """Return top-N attention items by priority (urgency x novelty x valence)."""
    from brain_core import attention as _att

    return {"items": _att.top_attention(limit=limit)}


@router.post("/brain/attention/enqueue", tags=["brain"])
def attention_enqueue(body: dict) -> dict:
    from brain_core import attention as _att

    return _att.enqueue(
        insight_id=str(body.get("id", "")),
        category=str(body.get("category", "pattern")),
        severity=str(body.get("severity", "info")),
        summary=str(body.get("summary", "")),
        detail=str(body.get("detail", "")),
        related_atoms=body.get("related_atoms") or [],
        ttl_hours=int(body.get("ttl_hours", 48)),
    )


@router.post("/brain/attention/{insight_id}/shown", tags=["brain"])
def attention_shown(insight_id: str) -> dict:
    from brain_core import attention as _att

    return _att.mark_shown(insight_id)


@router.post("/brain/attention/{insight_id}/dismiss", tags=["brain"])
def attention_dismiss(insight_id: str) -> dict:
    from brain_core import attention as _att

    return _att.dismiss(insight_id)


@router.get("/brain/attention/stats/summary", tags=["brain"])
def attention_stats() -> dict:
    from brain_core import attention as _att

    return _att.queue_stats()


# ── Predictive Action Model (biological: cerebellum, 2026-04-17) ───────
@router.get("/brain/predictive", tags=["brain"])
def predictive_top(limit: int = 3) -> dict:
    """Context-aware predictive prefetch based on current focus_items."""
    from brain_core import predictive as _p

    return {"items": _p.predict_relevant_context(limit=limit)}


@router.get("/brain/predictive/debug", tags=["brain"])
def predictive_debug() -> dict:
    """Inspect the exact focus signal driving the prediction."""
    from brain_core import predictive as _p

    return _p.debug_signal()


# ── Usage / budget ─────────────────────────────────────
@router.get("/brain/usage", tags=["brain"])
def brain_usage(days: int = Query(default=7, ge=1, le=365)) -> dict:
    """Usage stats — LLM dispatch budget + brain tool adoption."""
    out: dict = {"window_days": days}

    try:
        from brain_core import cli_llm

        out["llm"] = cli_llm.get_usage_stats(days=days)
    except Exception as e:
        out["llm"] = {"error": str(e)[:200], "source": "cli_llm"}

    try:
        from brain_core.atoms_store import action_audit_usage

        out["adoption"] = action_audit_usage(since_days=days)
    except Exception as e:
        out["adoption"] = {"error": str(e)[:200]}

    return out
