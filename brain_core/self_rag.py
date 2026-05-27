"""brain_core/self_rag.py — Self-RAG critique tokens (Asai et al. 2023).

2026-04-16 Tier 3 #11: the existing CRAG path uses a heuristic confidence
score (token overlap, source diversity, count) to decide whether the
initial retrieval is good enough or needs an iterative retry. The
heuristic is noise-prone — it declares confidence based on shape of
results, not their semantic fit to the query.

Self-RAG (Asai 2023) introduces three critique tokens per result:
  IsRel (is this document relevant?)
  IsSup (is the answer supported by this document?)
  IsUse (is this useful for answering the query?)

We implement a lite version: dispatch Jenna with the query + top-5
content snippets, ask for a YES/NO relevance flag per result plus a
single overall confidence float [0..1]. Replaces or augments
`_crag_score` at the CRAG decision point.

Off by default (BRAIN_SELF_RAG_ENABLED=false) since each invocation
costs one Jenna call (flat-rate via OpenClaw, but still a latency
hit ~1-2s). Enable selectively for critical queries, or once the
quality gain is confirmed by eval.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("brain.self_rag")

sys.path.insert(0, str(Path(__file__).resolve().parent))


@dataclass
class CritiqueReport:
    score: float  # overall confidence in [0, 1]
    per_result: list[dict]  # [{"idx": i, "relevant": bool, "note": str}]
    components: dict
    latency_ms: int


def _fallback_score(query: str, results: list[dict]) -> CritiqueReport:
    """Identity fallback — returns a neutral score when Jenna is unavailable."""
    return CritiqueReport(
        score=0.5,
        per_result=[],
        components={"source": "fallback"},
        latency_ms=0,
    )


_Y = re.compile(r"\byes\b", re.IGNORECASE)
_N = re.compile(r"\bno\b", re.IGNORECASE)


def critique(query: str, results: list[dict]) -> CritiqueReport:
    """Dispatch Jenna to score relevance of top-K results to the query.

    Returns a CritiqueReport with overall score + per-result relevance
    flags. Never raises — falls back to neutral score on any failure.
    """
    if os.environ.get("BRAIN_SELF_RAG_ENABLED", "").strip().lower() not in ("1", "true", "yes", "on"):
        return _fallback_score(query, results)
    if not results:
        return _fallback_score(query, results)

    # 2026-04-17: skip Jenna when Chris is actively in a Claude Code session.
    # Rationale: Claude is already providing recall quality judgement in its
    # own reasoning — spending 2-5s + Jenna tokens on a duplicate critique
    # adds latency without adding signal. When the session ends, later
    # /recall/v2 calls (from Telegram / Hermes profiles) resume Jenna path.
    try:
        from claude_session import is_session_active

        if is_session_active():
            return _fallback_score(query, results)
    except Exception as _exc:
        log.debug("silenced exception in self_rag.py: %s", _exc)

    t0 = time.time()
    try:
        top = results[:5]
        result_snippets = []
        for i, r in enumerate(top):
            title = (r.get("title") or "")[:80]
            content = (r.get("content") or "")[:280]
            result_snippets.append(f"[{i + 1}] {title}\n    {content}")
        prompt = (
            f"Score these search results for relevance to the query.\n\n"
            f"QUERY: {query}\n\n"
            f"RESULTS:\n" + "\n\n".join(result_snippets) + "\n\n"
            "For each result, answer YES or NO to: 'Does this directly "
            "help answer the query?'\n"
            "Then give an overall confidence 0.0-1.0 that the top results "
            "can answer the query.\n\n"
            "Output ONLY JSON: "
            '{"per_result": ["YES" or "NO", ...], "overall": 0.0}'
        )
        # 2026-04-17: migrated to cli_dispatch to avoid 95MB session replay
        from cli_llm import cli_dispatch

        r = cli_dispatch(prompt, backend="codex", timeout=30)
        if not getattr(r, "ok", False):
            return _fallback_score(query, results)
        text = (r.text or "").strip()
        # Strip code fences if Jenna wrapped
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # Fall back to regex extraction
            overall_match = re.search(r"overall[\"'\s:]+(0(?:\.\d+)?|1(?:\.0+)?)", text)
            overall = float(overall_match.group(1)) if overall_match else 0.5
            yes_count = len(_Y.findall(text))
            no_count = len(_N.findall(text))
            return CritiqueReport(
                score=overall,
                per_result=[
                    {"idx": i, "relevant": True, "note": "regex_parse"}
                    for i in range(min(yes_count, len(top)))
                ],
                components={"source": "self_rag_regex", "yes": yes_count, "no": no_count},
                latency_ms=int((time.time() - t0) * 1000),
            )
        per_result = []
        for i, flag in enumerate((parsed.get("per_result") or [])[: len(top)]):
            per_result.append({"idx": i, "relevant": str(flag).upper().startswith("Y")})
        overall = float(parsed.get("overall", 0.5))
        overall = max(0.0, min(1.0, overall))
        return CritiqueReport(
            score=overall,
            per_result=per_result,
            components={
                "source": "self_rag",
                "relevant_count": sum(1 for p in per_result if p.get("relevant")),
                "total": len(per_result),
            },
            latency_ms=int((time.time() - t0) * 1000),
        )
    except Exception:
        return _fallback_score(query, results)


def blend_with_heuristic(self_rag_score: float, heuristic_score: float) -> float:
    """Blend Self-RAG semantic score with CRAG heuristic score.

    Weighted average: Self-RAG is semantically correct but noisy at the
    edges; the heuristic catches shape issues. 65/35 favors Self-RAG
    when enabled.
    """
    return 0.65 * self_rag_score + 0.35 * heuristic_score
