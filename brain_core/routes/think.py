"""/chris/think — first-person decision endpoint routed to Jenna."""

from __future__ import annotations

import threading
import time

import rerank as _rerank
import rrf as _rrf
import search_unified
from api_deps import verify_bearer
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from metrics_buffer import metrics_buffer as _metrics_buf
from openclaw_dispatch import dispatch as _openclaw_dispatch
from profile_cache import profile_cache
from pydantic import BaseModel, Field

router = APIRouter(dependencies=[Depends(verify_bearer)])


class ThinkRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=1000)
    context: str | None = Field(default=None, max_length=2000)


class ThinkProvenance(BaseModel):
    id: str
    title: str
    source: str
    snippet: str


class ThinkResponse(BaseModel):
    question: str
    answer: str
    provenance: list[ThinkProvenance] = Field(default_factory=list)
    model: str = "jenna"
    latency_ms: int = 0


CHRIS_THINK_PROMPT = """You ARE Chris. Answer in first-person, direct, dry, no flattery. Match Chris's voice from his profile. Pretend you are his inner voice. No preamble, no "As Chris, I would". Just answer the question as if you're thinking out loud.

Chris's profile:
{profile}

Relevant recent preferences / facts / decisions:
{memories}

Recent context / schedule:
{context}

{extra_context}

Question: {question}

Your answer (one or two short paragraphs, first-person, no preamble):"""


_think_cache: dict[str, tuple[float, ThinkResponse]] = {}
_think_cache_lock = threading.Lock()
_THINK_CACHE_TTL = 60


def _compose_think_prompt(question: str, extra_context: str | None) -> tuple[str, list[dict]]:
    """Build the prompt + return the provenance list of memories cited."""
    profile_parts: list[str] = []
    for section in ("identity", "hard rules", "values", "tools", "workflow"):
        text = profile_cache.section(section)
        if text:
            profile_parts.append(text.strip())
    profile_text = "\n\n".join(profile_parts)[:3000] or "(profile unavailable)"

    provenance: list[dict] = []
    memory_lines: list[str] = []
    try:
        sm_payload = search_unified.search_all(
            question,
            8,
            sources=["rag", "canonical"],
            collections=["semantic_memory"],
            original_query=question,
        )
        rag_payload = search_unified.search_all(
            question,
            6,
            sources=["rag", "canonical"],
            original_query=question,
        )
        merged = _rrf.rrf_fuse(
            [sm_payload.get("results", []), rag_payload.get("results", [])],
            id_key="path",
        )
        merged = _rerank.rerank(question, merged, top_k=6)
        for m in merged:
            content = (m.get("content") or "")[:250]
            title = m.get("title") or m.get("collection") or ""
            memory_lines.append(f"- {content}")
            provenance.append(
                {
                    "id": m.get("path") or m.get("metadata", {}).get("id", ""),
                    "title": title[:120],
                    "source": m.get("collection", ""),
                    "snippet": content[:200],
                }
            )
    except Exception:  # noqa: S110 — best-effort context enrichment, never fatal
        pass
    memories_text = "\n".join(memory_lines) or "(no relevant memories found)"

    context_lines: list[str] = []
    try:
        cal_payload = search_unified.search_all(
            question,
            3,
            sources=["rag"],
            collections=["personal"],
            original_query=question,
        )
        for c in cal_payload.get("results", []):
            context_lines.append(f"- {c.get('content','')[:200]}")
    except Exception:  # noqa: S110 — best-effort context enrichment, never fatal
        pass
    context_text = "\n".join(context_lines) or "(no calendar context)"

    extra_text = f"Additional context from caller:\n{extra_context}" if extra_context else ""
    prompt = CHRIS_THINK_PROMPT.format(
        profile=profile_text,
        memories=memories_text,
        context=context_text,
        extra_context=extra_text,
        question=question,
    )
    return prompt, provenance


@router.post("/chris/think", response_model=ThinkResponse, tags=["decide"])
def chris_think(req: ThinkRequest, background: BackgroundTasks = None) -> ThinkResponse:
    """Ask Chris's second brain a decision question. Answers in first-person voice."""
    cache_key = f"{req.question}||{req.context or ''}"
    with _think_cache_lock:
        cached = _think_cache.get(cache_key)
        if cached and (time.time() - cached[0]) < _THINK_CACHE_TTL:
            return cached[1]

    t_start = time.time()
    prompt, provenance = _compose_think_prompt(req.question, req.context)

    dispatch_result = _openclaw_dispatch(
        agent="jenna",
        message=prompt,
        thinking="medium",
        timeout=90,
    )

    _metrics_buf.record_dispatch(
        ok=dispatch_result.ok,
        duration_ms=dispatch_result.duration_ms,
        rate_limited=dispatch_result.rate_limited,
        auth_failed=dispatch_result.auth_failed,
        attempts=dispatch_result.attempts,
    )

    if not dispatch_result.ok:
        detail = f"openclaw dispatch failed: {dispatch_result.error}"
        if dispatch_result.rate_limited:
            raise HTTPException(status_code=503, detail=f"rate_limited: {detail}")
        if dispatch_result.auth_failed:
            raise HTTPException(status_code=502, detail=f"auth_failed: {detail}")
        raise HTTPException(status_code=502, detail=detail)

    answer = dispatch_result.text.strip()
    if not answer:
        raise HTTPException(status_code=502, detail="openclaw returned empty answer")

    response = ThinkResponse(
        question=req.question,
        answer=answer,
        provenance=[ThinkProvenance(**p) for p in provenance[:6]],
        model=dispatch_result.model or "jenna",
        latency_ms=int((time.time() - t_start) * 1000),
    )

    with _think_cache_lock:
        _think_cache[cache_key] = (time.time(), response)
        if len(_think_cache) > 64:
            oldest = min(_think_cache, key=lambda k: _think_cache[k][0])
            _think_cache.pop(oldest, None)

    if background is not None:

        def _record_candidate() -> None:
            try:
                import answer_candidates as _ac

                _ac.record(
                    source_route="/chris/think",
                    query=req.question,
                    answer=answer,
                    agent="chris",
                    reason=req.context,
                )
            except Exception:  # noqa: S110 — background candidate write is best-effort
                pass

        background.add_task(_record_candidate)

    return response
