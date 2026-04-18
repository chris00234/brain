"""brain_core/ragas_judge.py — LLM-as-judge RAG eval (M8.3).

Implements the four core RAGAS metrics (https://docs.ragas.io) using the
existing openclaw_dispatch path so we don't pay extra LLM cost — every call
goes through Chris's existing OpenAI subscription via Sage.

Metrics:
  - faithfulness        Are the claims in the answer actually supported by
                        the retrieved context? (0.0-1.0)
  - answer_relevance    Does the answer actually address the user's question?
                        (0.0-1.0)
  - context_precision   Of the retrieved chunks, what fraction were actually
                        useful for answering? (0.0-1.0)
  - context_recall      Of the ground-truth answer's facts, what fraction
                        were retrievable from the context? (0.0-1.0)

These four together tell you not just "did we retrieve the right doc?"
(which hit_content already measures) but "did we retrieve the right doc
AND give it to the LLM correctly AND let the LLM answer correctly?" —
which is the actual contract for any RAG system.

Cost: 1-4 LLM dispatches per (query, answer) pair through sage via
openclaw_dispatch. Model is whatever sage is configured with in
~/.openclaw/openclaw.json (gpt-5.4 primary with gpt-5.3-codex-spark /
claude-opus-4-6 fallback chain as of 2026-04-14).

Default OFF — only fires when called explicitly from cli/eval_compare.py
with --ragas. Not on the hot path.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

log = logging.getLogger("brain.ragas_judge")


@dataclass
class RagasScore:
    faithfulness: float | None = None
    answer_relevance: float | None = None
    context_precision: float | None = None
    context_recall: float | None = None
    notes: list[str] | None = None

    def to_dict(self) -> dict:
        return {
            "faithfulness": self.faithfulness,
            "answer_relevance": self.answer_relevance,
            "context_precision": self.context_precision,
            "context_recall": self.context_recall,
            "notes": self.notes or [],
        }


# ── Prompts (kept short — Sage handles structured output reliably) ──

_FAITHFULNESS_PROMPT = """You are a RAG faithfulness judge. Given a question, an answer, and the retrieved context, decide what fraction of the answer's claims are directly supported by the context.

Question: {question}
Context:
{context}
Answer: {answer}

Output ONLY a JSON object: {{"score": <float 0.0-1.0>, "reason": "<one sentence>"}}
- 1.0 = every claim in the answer is supported by the context
- 0.5 = roughly half the claims are supported
- 0.0 = the answer is hallucinated or contradicts the context"""

_ANSWER_RELEVANCE_PROMPT = """You are a RAG relevance judge. Decide whether the answer actually addresses the question (regardless of whether it's correct).

Question: {question}
Answer: {answer}

Output ONLY a JSON object: {{"score": <float 0.0-1.0>, "reason": "<one sentence>"}}
- 1.0 = directly answers the question
- 0.5 = partially addresses the question
- 0.0 = ignores the question or answers a different one"""

_CONTEXT_PRECISION_PROMPT = """You are a RAG context precision judge. For each numbered context chunk, decide whether it was useful for answering the question.

Question: {question}
Expected answer (ground truth): {expected}

Context chunks:
{numbered_context}

Output ONLY a JSON object: {{"useful_indices": [<list of 1-indexed chunk numbers that were useful>], "reason": "<one sentence>"}}"""

_CONTEXT_RECALL_PROMPT = """You are a RAG context recall judge. Decide what fraction of the expected answer's claims could be derived from the retrieved context.

Question: {question}
Expected answer: {expected}
Retrieved context:
{context}

Output ONLY a JSON object: {{"score": <float 0.0-1.0>, "reason": "<one sentence>"}}
- 1.0 = every fact in the expected answer is present in the context
- 0.5 = half the facts are present
- 0.0 = the expected answer cannot be derived from the context at all"""


def _dispatch_judge(prompt: str, *, timeout: int = 30) -> str | None:
    """Send a judge prompt to Sage via openclaw_dispatch. Returns text or None."""
    try:
        from cli_llm import dispatch

        result = dispatch("sage", prompt, thinking="off", timeout=timeout)
        if result.ok and result.text:
            return result.text.strip()
    except Exception as exc:
        log.warning("ragas_judge dispatch failed: %s", exc)
    return None


_JSON_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)


def _parse_score(raw: str | None) -> tuple[float | None, str]:
    if not raw:
        return None, "no response"
    # Try direct JSON parse first
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        m = _JSON_RE.search(raw)
        if not m:
            return None, "no json found"
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None, "json parse failed"
    score = obj.get("score")
    reason = obj.get("reason", "")
    if score is None:
        return None, reason or "no score key"
    try:
        return max(0.0, min(1.0, float(score))), reason
    except (TypeError, ValueError):
        return None, "score not numeric"


def _parse_useful_indices(raw: str | None, n_chunks: int) -> tuple[float | None, str]:
    if not raw:
        return None, "no response"
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        m = _JSON_RE.search(raw)
        if not m:
            return None, "no json found"
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None, "json parse failed"
    indices = obj.get("useful_indices") or []
    reason = obj.get("reason", "")
    if not isinstance(indices, list) or n_chunks <= 0:
        return None, reason or "no indices"
    valid = [i for i in indices if isinstance(i, int) and 1 <= i <= n_chunks]
    return len(valid) / n_chunks, reason


def score_one(
    question: str,
    answer: str,
    context_chunks: list[str],
    expected: str | None = None,
    *,
    metrics: list[str] | None = None,
    timeout: int = 30,
) -> RagasScore:
    """Score a single (question, answer, context) triple.

    metrics defaults to all four. Pass a subset like
    ["faithfulness", "answer_relevance"] to skip the expensive ones.
    """
    metrics = metrics or [
        "faithfulness",
        "answer_relevance",
        "context_precision",
        "context_recall",
    ]
    score = RagasScore(notes=[])
    context_text = "\n---\n".join(context_chunks)[:4000]
    numbered_context = "\n".join(f"{i + 1}. {chunk[:600]}" for i, chunk in enumerate(context_chunks))

    if "faithfulness" in metrics:
        raw = _dispatch_judge(
            _FAITHFULNESS_PROMPT.format(question=question[:500], context=context_text, answer=answer[:1000]),
            timeout=timeout,
        )
        s, reason = _parse_score(raw)
        score.faithfulness = s
        if reason:
            score.notes.append(f"faithfulness: {reason}")

    if "answer_relevance" in metrics:
        raw = _dispatch_judge(
            _ANSWER_RELEVANCE_PROMPT.format(question=question[:500], answer=answer[:1000]),
            timeout=timeout,
        )
        s, reason = _parse_score(raw)
        score.answer_relevance = s
        if reason:
            score.notes.append(f"answer_relevance: {reason}")

    if "context_precision" in metrics and context_chunks and expected:
        raw = _dispatch_judge(
            _CONTEXT_PRECISION_PROMPT.format(
                question=question[:500],
                expected=expected[:500],
                numbered_context=numbered_context,
            ),
            timeout=timeout,
        )
        s, reason = _parse_useful_indices(raw, len(context_chunks))
        score.context_precision = s
        if reason:
            score.notes.append(f"context_precision: {reason}")

    if "context_recall" in metrics and expected:
        raw = _dispatch_judge(
            _CONTEXT_RECALL_PROMPT.format(
                question=question[:500], expected=expected[:500], context=context_text
            ),
            timeout=timeout,
        )
        s, reason = _parse_score(raw)
        score.context_recall = s
        if reason:
            score.notes.append(f"context_recall: {reason}")

    return score


def aggregate(scores: list[RagasScore]) -> dict:
    """Mean of each metric across a list of single-query scores."""

    def _mean(key: str) -> float | None:
        values = [getattr(s, key) for s in scores if getattr(s, key) is not None]
        if not values:
            return None
        return round(sum(values) / len(values), 3)

    return {
        "n": len(scores),
        "faithfulness_mean": _mean("faithfulness"),
        "answer_relevance_mean": _mean("answer_relevance"),
        "context_precision_mean": _mean("context_precision"),
        "context_recall_mean": _mean("context_recall"),
    }


def stats() -> dict:
    return {
        "metrics": ["faithfulness", "answer_relevance", "context_precision", "context_recall"],
        "judge_model": "sage (openclaw_dispatch, configured fallback chain)",
        "default_timeout_s": 30,
    }
