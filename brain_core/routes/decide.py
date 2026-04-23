"""/brain/decide + /brain/reason — preference-grounded decision + multi-step reasoning."""

from __future__ import annotations

import hashlib
import json as _json
import time
from datetime import UTC, datetime

from api_deps import _log_failure, _safe_http_detail, verify_bearer
from config import BRAIN_DIR
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from rate_limit import limiter

router = APIRouter(dependencies=[Depends(verify_bearer)])


class DecideRequest(BaseModel):
    situation: str = Field(..., min_length=10, max_length=2000)
    options: list[dict] = Field(..., min_length=2, max_length=6)
    agent: str = Field(default="claude", max_length=32)
    domain: str | None = Field(default=None)
    context: str | None = Field(default=None, max_length=2000)


class DecideResponse(BaseModel):
    situation: str
    recommendation: str
    reasoning: str
    confidence: float
    evidence: list[dict] = Field(default_factory=list)
    exceptions: list[str] = Field(default_factory=list)
    model: str = "sage"
    latency_ms: int = 0
    cached: bool = False
    heuristic_fallback: bool = False


class ReasonRequest(BaseModel):
    question: str = Field(..., min_length=5, max_length=2000)
    context: str | None = Field(default=None, max_length=3000)
    agent: str = Field(default="claude", max_length=32)
    domain: str | None = None


class ReasonResponse(BaseModel):
    question: str
    analysis: str
    reasoning_steps: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    provenance: list[dict] = Field(default_factory=list)
    model: str = "sage"
    latency_ms: int = 0


def _persist_reasoning_result(title: str, content: str, domain: str, confidence: float) -> None:
    """Persist high-confidence analysis as a distilled note so it accumulates across sessions."""
    if confidence < 0.7:
        return
    try:
        slug = hashlib.md5(title.encode()).hexdigest()[:12]  # noqa: S324 — non-crypto slug
        note_path = BRAIN_DIR.parent / "knowledge" / "distilled" / "decisions" / f"brain_analysis_{slug}.md"
        if note_path.exists():
            return
        note_path.parent.mkdir(parents=True, exist_ok=True)
        meta = {
            "id": f"dist_brain_analysis_{slug}",
            "type": "distilled",
            "domain": domain or "decisions",
            "subtype": "brain-analysis",
            "title": title[:120],
            "status": "active",
            "confidence": round(confidence, 2),
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "sources": ["brain_reasoning_api"],
        }
        with note_path.open("w") as f:
            f.write("---json\n")
            f.write(_json.dumps(meta, indent=2, ensure_ascii=False))
            f.write("\n---\n\n")
            f.write(content[:2000])
    except Exception:  # noqa: S110 — persistence must never break the API response
        pass


@router.post("/brain/decide", response_model=DecideResponse, tags=["decide"])
@limiter.limit("60/minute")
def brain_decide(request: Request, req: DecideRequest) -> DecideResponse:
    """Agent asks brain for a structured decision recommendation."""
    start = time.time()
    try:
        from brain_core.reasoning import DecisionOption, evaluate_decision

        options = [
            DecisionOption(label=o.get("label", ""), description=o.get("description", ""))
            for o in req.options
        ]
        result = evaluate_decision(req.situation, options, req.agent, req.domain)
        evidence = [
            {
                "content": h.content[:200],
                "category": h.category,
                "confidence": h.confidence,
                "source": h.source,
            }
            for h in result.preference_hits[:5]
        ]
        resp = DecideResponse(
            situation=req.situation,
            recommendation=result.recommendation,
            reasoning=result.reasoning,
            confidence=result.confidence,
            evidence=evidence,
            exceptions=result.exceptions,
            model=result.model,
            latency_ms=int((time.time() - start) * 1000),
            cached=result.cached,
            heuristic_fallback=result.heuristic_fallback,
        )
        _persist_reasoning_result(
            f"Decision: {req.situation[:80]} → {result.recommendation}",
            f"## Situation\n{req.situation}\n\n## Recommendation\n{result.recommendation}"
            f"\n\n## Reasoning\n{result.reasoning}",
            req.domain or "decisions",
            result.confidence,
        )
        return resp
    except Exception as e:
        _log_failure(str(e)[:500], route="/brain/decide")
        raise HTTPException(
            status_code=500, detail=_safe_http_detail("decide", e, route="/brain/decide")
        ) from e


@router.post("/brain/reason", response_model=ReasonResponse, tags=["decide"])
@limiter.limit("60/minute")
def brain_reason(request: Request, req: ReasonRequest) -> ReasonResponse:
    """Deeper multi-step reasoning for complex questions."""
    start = time.time()
    try:
        from brain_core.reasoning import reason_deep

        result = reason_deep(req.question, req.context, req.agent, req.domain)
        resp = ReasonResponse(
            question=req.question,
            analysis=getattr(result, "answer", ""),
            reasoning_steps=getattr(result, "reasoning_steps", []),
            confidence=getattr(result, "confidence", 0.0),
            provenance=[vars(p) if hasattr(p, "__dict__") else p for p in getattr(result, "provenance", [])],
            model=getattr(result, "model", "sage"),
            latency_ms=int((time.time() - start) * 1000),
        )
        _persist_reasoning_result(
            f"Analysis: {req.question[:80]}",
            f"## Question\n{req.question}\n\n## Analysis\n{getattr(result, 'answer', '')}",
            req.domain or "analysis",
            getattr(result, "confidence", 0.0),
        )
        return resp
    except Exception as e:
        _log_failure(str(e)[:500], route="/brain/reason")
        raise HTTPException(
            status_code=500, detail=_safe_http_detail("reason", e, route="/brain/reason")
        ) from e
