"""brain_core/reasoning.py — structured decision engine for Chris's brain system.

When an agent faces a choice, it queries the brain which evaluates options
against Chris's known preferences, past decisions, and values.

All recommendations are SUGGESTIONS. The response includes reasoning and
confidence so agents can present:
  "Based on your preferences, I'd do X because Y. Should I proceed?"

Usage:
    from reasoning import evaluate_decision, suggest_delegation, reason_deep

    result = evaluate_decision(
        situation="Choosing between Redis and Memcached for session cache",
        options=[
            DecisionOption("redis", "Persistent, richer data structures"),
            DecisionOption("memcached", "Simpler, lower latency"),
        ],
    )
    # result.recommendation, result.reasoning, result.confidence
"""

from __future__ import annotations

import copy
import json
import logging
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

import rerank
import rrf
import search_unified
import time_decay
from boot_context import get_chris_state as get_chris_profile  # 2026-04-17 — alias at import-site
from cli_llm import dispatch  # migrated 2026-04-17

log = logging.getLogger("brain.reasoning")

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class DecisionOption:
    label: str
    description: str = ""


@dataclass
class PreferenceHit:
    content: str
    category: str
    confidence: float
    age_days: float
    source: str
    collection: str


@dataclass
class DecisionResult:
    recommendation: str
    reasoning: str
    confidence: float
    preference_hits: list[PreferenceHit] = field(default_factory=list)
    exceptions: list[str] = field(default_factory=list)
    model: str = "sage"
    latency_ms: int = 0
    cached: bool = False
    heuristic_fallback: bool = False


# ---------------------------------------------------------------------------
# Cache (mirrors server.py _think_cache pattern)
# ---------------------------------------------------------------------------

_decide_cache: dict[str, tuple[float, DecisionResult]] = {}
_decide_cache_lock = threading.Lock()
_DECIDE_CACHE_TTL = 300  # 5 minutes
_MAX_CACHE_SIZE = 64


def _cache_get(key: str) -> DecisionResult | None:
    with _decide_cache_lock:
        entry = _decide_cache.get(key)
        if not entry:
            return None
        if (time.time() - entry[0]) >= _DECIDE_CACHE_TTL:
            _decide_cache.pop(key, None)
            return None
        result = copy.deepcopy(entry[1])
        result.cached = True
        return result


def _cache_put(key: str, result: DecisionResult) -> None:
    with _decide_cache_lock:
        _decide_cache[key] = (time.time(), copy.deepcopy(result))
        while len(_decide_cache) > _MAX_CACHE_SIZE:
            oldest = min(_decide_cache, key=lambda k: _decide_cache[k][0])
            _decide_cache.pop(oldest, None)


# ---------------------------------------------------------------------------
# 1. gather_decision_context
# ---------------------------------------------------------------------------


def gather_decision_context(
    situation: str,
    options: list[DecisionOption],
    agent: str,
    domain: str | None = None,
) -> tuple[list[PreferenceHit], str, str]:
    """Search memories, canonical knowledge, and experience for decision-relevant context.

    Returns (preference_hits, profile_text, context_text).
    """
    now = datetime.now(UTC)

    # Fan out three searches in parallel
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=3) as pool:
        f_sem = pool.submit(_search_collection, situation, 10, ["semantic_memory"], None)
        f_can = pool.submit(_search_collection, situation, 10, ["canonical"], domain)
        f_exp = pool.submit(_search_collection, situation, 10, ["experience"], None)
        semantic_results = f_sem.result()
        canonical_results = f_can.result()
        experience_results = f_exp.result()

    # RRF-fuse all three sources
    fused = rrf.rrf_fuse(
        result_lists=[semantic_results, canonical_results, experience_results],
        trust_weights=[0.8, 1.0, 0.85],
        id_key="path",
    )

    # Rerank against the situation text
    reranked = rerank.rerank(situation, fused, top_k=12)
    for r in reranked:
        r["score"] = r.get("rerank_score", r.get("score", 0))

    # Apply time decay
    time_decay.apply_to_results(reranked)

    # Re-sort after decay (decay modifies score in-place)
    reranked.sort(key=lambda r: r.get("score", 0), reverse=True)

    # Build PreferenceHit list from top 8
    hits: list[PreferenceHit] = []
    for r in reranked[:8]:
        created_at = r.get("created_at") or (r.get("metadata") or {}).get("created_at")
        age_days = _age_days(created_at, now)
        hits.append(
            PreferenceHit(
                content=(r.get("content") or r.get("title") or "")[:500],
                category=(r.get("metadata") or {}).get("category", r.get("collection", "unknown")),
                confidence=float(r.get("score", 0)),
                age_days=age_days,
                source=r.get("source", r.get("collection", "unknown")),
                collection=r.get("collection", "unknown"),
            )
        )

    # Pull Chris's profile
    profile_text = get_chris_profile() or ""

    # Build compact context string from options
    option_lines = [f"- {o.label}: {o.description}" if o.description else f"- {o.label}" for o in options]
    context_text = f"Agent: {agent}\nOptions:\n" + "\n".join(option_lines)

    return hits, profile_text, context_text


def _search_collection(
    query: str,
    limit: int = 10,
    collections: list[str] | None = None,
    domain: str | None = None,
) -> list[dict[str, Any]]:
    """Thin wrapper around search_unified.search_all, returns results list."""
    try:
        payload = search_unified.search_all(
            query,
            limit=limit,
            collections=collections,
            domain=domain,
        )
        return payload.get("results", []) if isinstance(payload, dict) else []
    except Exception as exc:
        log.warning("search failed (collections=%s): %s", collections, exc)
        return []


def _age_days(created_at: Any, now: datetime) -> float:
    if not created_at:
        return 0.0
    try:
        if isinstance(created_at, str):
            txt = created_at.replace("Z", "+00:00")
            dt = datetime.fromisoformat(txt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
        elif isinstance(created_at, datetime):
            dt = created_at if created_at.tzinfo else created_at.replace(tzinfo=UTC)
        else:
            return 0.0
        return max(0.0, (now - dt).total_seconds() / 86400.0)
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# 2. build_decision_prompt
# ---------------------------------------------------------------------------


def build_decision_prompt(
    situation: str,
    options: list[DecisionOption],
    hits: list[PreferenceHit],
    profile_text: str,
    context_text: str,
    agent: str,
) -> str:
    """Compose a structured prompt for Sage to evaluate the decision."""
    sections: list[str] = []

    # Profile
    if profile_text:
        sections.append(f"## Chris's Profile\n{profile_text[:1500]}")

    # Evidence
    if hits:
        evidence_lines = []
        for i, h in enumerate(hits):
            evidence_lines.append(
                f"[{i+1}] ({h.category}, {h.age_days:.0f}d ago, conf={h.confidence:.1f}) {h.content}"
            )
        sections.append("## Evidence from Memory\n" + "\n".join(evidence_lines))

    # Situation
    sections.append(f"## Situation\n{situation}")

    # Options
    option_lines = []
    for o in options:
        desc = f": {o.description}" if o.description else ""
        option_lines.append(f"- **{o.label}**{desc}")
    sections.append("## Options\n" + "\n".join(option_lines))

    # Context
    sections.append(f"## Context\n{context_text}")

    # Instructions
    sections.append("""## Task
Based on the evidence above, recommend which option Chris would prefer.

Respond with ONLY a JSON object (no markdown fences):
{
  "recommendation": "<option label>",
  "reasoning": "<1-3 sentences explaining why, referencing evidence by [index]>",
  "confidence": <0.0-1.0>,
  "exceptions": ["<scenario where this recommendation might not apply>"]
}

Rules:
- Ground reasoning in the evidence. If no evidence is relevant, say so and lower confidence.
- confidence > 0.8 only if multiple evidence items strongly agree.
- This is a SUGGESTION — Chris makes the final call.""")

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# 3. evaluate_decision
# ---------------------------------------------------------------------------


def evaluate_decision(
    situation: str,
    options: list[DecisionOption],
    agent: str = "claude",
    domain: str | None = None,
    timeout: int = 60,
) -> DecisionResult:
    """Main entry point. Evaluates options against Chris's preferences.

    Checks TTL cache, gathers context, dispatches to Sage, falls back to
    heuristic keyword voting if Sage is unavailable.
    """
    if not options:
        raise ValueError("evaluate_decision requires at least one option")
    cache_key = f"{situation}||{'|'.join(o.label for o in options)}||{domain or ''}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    t_start = time.time()

    # Gather context
    hits, profile_text, context_text = gather_decision_context(situation, options, agent, domain)

    # Build prompt
    prompt = build_decision_prompt(situation, options, hits, profile_text, context_text, agent)

    # SOFAI-LM two-tier confidence: try fast agent (Jenna) first, escalate to Sage if uncertain
    result: DecisionResult | None = None

    # Tier 1: Fast evaluation via Jenna (cheap, thinking=off)
    dispatch_result = dispatch(
        agent="jenna",
        message=prompt,
        thinking="off",
        timeout=min(timeout, 30),
    )
    if dispatch_result.ok and dispatch_result.text:
        result = _parse_sage_response(dispatch_result.text, hits)
        if result:
            result.model = f"tier1:{dispatch_result.model or 'jenna'}"

    # Tier 2: If Tier 1 failed or confidence is low, escalate to Sage (expensive, thinking=low)
    if result is None or result.confidence < 0.6:
        log.info(
            "SOFAI-LM: escalating to Sage (tier1 confidence=%s)", result.confidence if result else "failed"
        )
        sage_result = dispatch(
            agent="sage",
            message=prompt,
            thinking="low",
            timeout=timeout,
        )
        if sage_result.ok and sage_result.text:
            tier2 = _parse_sage_response(sage_result.text, hits)
            if tier2:
                tier2.model = f"tier2:{sage_result.model or 'sage'}"
                result = tier2

    # Heuristic fallback if both tiers failed
    if result is None:
        log.warning("Both tiers failed, using heuristic fallback")
        result = _heuristic_decision(situation, options, hits)

    result.preference_hits = hits
    result.latency_ms = int((time.time() - t_start) * 1000)

    _cache_put(cache_key, result)
    return result


def _extract_json_object(text: str) -> str | None:
    """Find the first valid JSON object in text using json.JSONDecoder (brace-in-string safe)."""
    start = text.find("{")
    if start == -1:
        return None
    try:
        decoder = json.JSONDecoder()
        obj, _ = decoder.raw_decode(text, start)
        return json.dumps(obj)
    except (json.JSONDecodeError, ValueError):
        return None


def _extract_json_array(text: str) -> str | None:
    """Find the first valid JSON array in text using json.JSONDecoder (bracket-in-string safe)."""
    start = text.find("[")
    if start == -1:
        return None
    try:
        decoder = json.JSONDecoder()
        arr, _ = decoder.raw_decode(text, start)
        return json.dumps(arr)
    except (json.JSONDecodeError, ValueError):
        return None


def _parse_sage_response(text: str, hits: list[PreferenceHit]) -> DecisionResult | None:
    """Extract JSON from Sage's response. Tolerates markdown fences."""
    # Strip markdown code fences if present
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        extracted = _extract_json_object(text)
        if not extracted:
            log.warning("Could not parse Sage response as JSON")
            return None
        try:
            data = json.loads(extracted)
        except json.JSONDecodeError:
            return None

    recommendation = data.get("recommendation", "")
    reasoning = data.get("reasoning", "")
    confidence = float(data.get("confidence", 0.5))
    exceptions = data.get("exceptions", [])

    if not recommendation:
        return None

    return DecisionResult(
        recommendation=recommendation,
        reasoning=reasoning,
        confidence=min(1.0, max(0.0, confidence)),
        exceptions=exceptions if isinstance(exceptions, list) else [str(exceptions)],
    )


# ---------------------------------------------------------------------------
# Heuristic fallback — keyword-weighted preference vote
# ---------------------------------------------------------------------------

# Keyword signals per common domain. Extend as patterns emerge.
_DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "infrastructure": [
        "docker",
        "container",
        "nginx",
        "cloudflare",
        "homelab",
        "launchd",
        "native",
        "orbstack",
    ],
    "frontend": ["nextjs", "react", "tailwind", "shadcn", "typescript", "vite", "app router"],
    "backend": ["fastapi", "python", "api", "endpoint", "uvicorn"],
    "data": ["chromadb", "chroma", "ollama", "embedding", "vector", "rag"],
    "workflow": ["agent", "openclaw", "jenna", "liz", "ellie", "sage", "market", "dispatch"],
}


def _heuristic_decision(
    situation: str,
    options: list[DecisionOption],
    hits: list[PreferenceHit],
) -> DecisionResult:
    """Pure-heuristic fallback: score options by keyword overlap with preference hits."""
    situation_lower = situation.lower()

    # Collect all evidence text
    evidence_text = " ".join(h.content.lower() for h in hits)

    scores: dict[str, float] = {}
    for option in options:
        label_lower = option.label.lower()
        desc_lower = (option.description or "").lower()
        option_tokens = set(re.findall(r"[a-z0-9_\-]{2,}", f"{label_lower} {desc_lower}"))

        score = 0.0
        for token in option_tokens:
            # Count mentions in evidence (weighted by recency via position)
            count = evidence_text.count(token)
            score += count * 1.0

            # Bonus if token appears in situation
            if token in situation_lower:
                score += 0.5

        scores[option.label] = score

    if not scores or max(scores.values()) == 0:
        # No signal — pick first option with low confidence
        return DecisionResult(
            recommendation=options[0].label if options else "",
            reasoning="No relevant evidence found in memory. This is a low-confidence default.",
            confidence=0.2,
            heuristic_fallback=True,
        )

    best_label = max(scores, key=scores.get)  # type: ignore[arg-type]
    best_score = scores[best_label]
    total = sum(scores.values())
    margin = best_score / total if total > 0 else 0.0

    # Confidence based on margin and evidence count
    confidence = min(0.7, 0.3 + margin * 0.4 + min(len(hits), 5) * 0.05)

    return DecisionResult(
        recommendation=best_label,
        reasoning=f"Keyword analysis of {len(hits)} memory hits favors '{best_label}' "
        f"(score: {best_score:.1f}/{total:.1f}). No LLM verification — treat as weak signal.",
        confidence=confidence,
        heuristic_fallback=True,
    )


# ---------------------------------------------------------------------------
# 4. suggest_delegation
# ---------------------------------------------------------------------------

_AGENT_KEYWORDS: dict[str, list[str]] = {
    "liz": [
        "code",
        "implement",
        "build",
        "debug",
        "fix",
        "refactor",
        "test",
        "typescript",
        "python",
        "react",
        "nextjs",
        "fastapi",
        "frontend",
        "backend",
        "api",
        "feature",
        "pull request",
        "pr",
        "git",
        "architecture",
        "design pattern",
        "component",
    ],
    "ellie": [
        "docker",
        "container",
        "deploy",
        "infra",
        "infrastructure",
        "nginx",
        "cloudflare",
        "server",
        "homelab",
        "orbstack",
        "launchd",
        "service",
        "uptime",
        "monitoring",
        "dns",
        "ssl",
        "certificate",
        "network",
        "port",
        "firewall",
        "backup",
    ],
    "jenna": [
        "schedule",
        "calendar",
        "reminder",
        "task",
        "organize",
        "prioritize",
        "plan",
        "coordinate",
        "message",
        "email",
        "communication",
        "meeting",
        "daily",
        "weekly",
        "status",
        "report",
        "summary",
        "brief",
        "delegate",
    ],
    "sage": [
        "research",
        "analyze",
        "compare",
        "evaluate",
        "investigate",
        "study",
        "learn",
        "knowledge",
        "synthesis",
        "pattern",
        "contradiction",
        "deep dive",
        "explore",
        "question",
        "reasoning",
        "think",
        "reflect",
        "strategy",
    ],
    "market": [
        "content",
        "blog",
        "post",
        "seo",
        "analytics",
        "brand",
        "marketing",
        "ghost",
        "social",
        "growth",
        "audience",
        "writing",
        "article",
        "publish",
        "newsletter",
    ],
}


def suggest_delegation(task_description: str) -> dict[str, Any]:
    """Hybrid routing: try learned routing first (from outcome data), fall back to keyword heuristic.

    Returns {"agent": str, "confidence": float, "reasoning": str}.
    """
    # Try learned routing first (MasRouter pattern — uses past outcome data)
    try:
        from task_queue import task_queue

        learned = task_queue.suggest_delegation_learned(task_description)
        if learned:
            return learned
    except Exception:
        pass

    # Fall back to keyword heuristic
    task_lower = task_description.lower()
    task_tokens = set(re.findall(r"[a-z0-9_\-]{2,}", task_lower))

    scores: dict[str, float] = {}
    matched_keywords: dict[str, list[str]] = {}

    for agent_name, keywords in _AGENT_KEYWORDS.items():
        matched = []
        score = 0.0
        for kw in keywords:
            if kw in task_lower:
                # Multi-word keywords get a bonus
                weight = 2.0 if " " in kw else 1.0
                score += weight
                matched.append(kw)
            else:
                # Check individual token overlap
                kw_tokens = set(kw.split())
                overlap = kw_tokens & task_tokens
                if overlap:
                    score += 0.5 * len(overlap)
                    matched.extend(overlap)

        scores[agent_name] = score
        matched_keywords[agent_name] = matched

    if not scores or max(scores.values()) == 0:
        return {
            "agent": "jenna",
            "confidence": 0.2,
            "reasoning": "No strong keyword match. Defaulting to Jenna (Chief of Staff) for triage.",
        }

    best = max(scores, key=scores.get)  # type: ignore[arg-type]
    best_score = scores[best]
    total = sum(scores.values())
    margin = best_score / total if total > 0 else 0.0

    # Second-best for reasoning
    sorted_agents = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    runner_up = sorted_agents[1][0] if len(sorted_agents) > 1 else None

    confidence = min(0.95, 0.3 + margin * 0.5 + min(best_score, 8) * 0.05)
    keywords_str = ", ".join(matched_keywords[best][:5])
    reasoning = f"Matched keywords: [{keywords_str}] (score: {best_score:.1f}/{total:.1f})."
    if runner_up and scores[runner_up] > 0:
        reasoning += f" Runner-up: {runner_up} ({scores[runner_up]:.1f})."

    return {
        "agent": best,
        "confidence": round(confidence, 2),
        "reasoning": reasoning,
    }


# ---------------------------------------------------------------------------
# 5. reason_deep
# ---------------------------------------------------------------------------


@dataclass
class DeepReasoningResult:
    answer: str
    reasoning_steps: list[str] = field(default_factory=list)
    provenance: list[PreferenceHit] = field(default_factory=list)
    confidence: float = 0.0
    model: str = "sage"
    latency_ms: int = 0


def reason_deep(
    question: str,
    context: str | None = None,
    agent: str = "claude",
    domain: str | None = None,
    timeout: int = 90,
) -> DeepReasoningResult:
    """Deeper analysis with chain-of-thought via Sage (thinking=high).

    Gathers memory context, dispatches to Sage for step-by-step reasoning,
    returns structured steps + provenance.
    """
    t_start = time.time()

    # Gather relevant context — fan out per collection in parallel and RRF-fuse
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=3) as pool:
        f_sem = pool.submit(_search_collection, question, 8, ["semantic_memory"], None)
        f_can = pool.submit(_search_collection, question, 8, ["canonical"], domain)
        f_exp = pool.submit(_search_collection, question, 8, ["experience"], None)
        semantic_results = f_sem.result()
        canonical_results = f_can.result()
        experience_results = f_exp.result()

    fused = rrf.rrf_fuse(
        result_lists=[semantic_results, canonical_results, experience_results],
        trust_weights=[0.8, 1.0, 0.85],
        id_key="path",
    )
    reranked = rerank.rerank(question, fused, top_k=10)
    for r in reranked:
        r["score"] = r.get("rerank_score", r.get("score", 0))
    time_decay.apply_to_results(reranked)
    memory_results = sorted(reranked, key=lambda r: r.get("score", 0), reverse=True)

    now = datetime.now(UTC)
    provenance: list[PreferenceHit] = []
    for r in memory_results[:6]:
        created_at = r.get("created_at") or (r.get("metadata") or {}).get("created_at")
        provenance.append(
            PreferenceHit(
                content=(r.get("content") or r.get("title") or "")[:500],
                category=(r.get("metadata") or {}).get("category", r.get("collection", "unknown")),
                confidence=float(r.get("score", 0)),
                age_days=_age_days(created_at, now),
                source=r.get("source", r.get("collection", "unknown")),
                collection=r.get("collection", "unknown"),
            )
        )

    profile_text = get_chris_profile() or ""

    # Build deep reasoning prompt
    sections: list[str] = []
    if profile_text:
        sections.append(f"## Chris's Profile\n{profile_text[:1500]}")
    if provenance:
        evidence_lines = [
            f"[{i+1}] ({p.category}, {p.age_days:.0f}d ago) {p.content}" for i, p in enumerate(provenance)
        ]
        sections.append("## Evidence\n" + "\n".join(evidence_lines))
    if context:
        sections.append(f"## Additional Context\n{context}")

    sections.append(f"## Question\n{question}")
    sections.append("""## Task
Think through this step by step. Consider Chris's preferences and past decisions from the evidence.

Respond with ONLY a JSON object (no markdown fences):
{
  "answer": "<your conclusion>",
  "confidence": <0.0-1.0>,
  "steps": [
    "<step 1: what you considered>",
    "<step 2: what evidence suggests>",
    "<step 3: your reasoning>"
  ]
}""")

    prompt = "\n\n".join(sections)

    dispatch_result = dispatch(
        agent="sage",
        message=prompt,
        thinking="high",
        timeout=timeout,
    )

    if dispatch_result.ok and dispatch_result.text:
        parsed = _parse_deep_response(dispatch_result.text)
        if parsed:
            return DeepReasoningResult(
                answer=parsed["answer"],
                reasoning_steps=parsed["steps"],
                provenance=provenance,
                confidence=parsed.get("confidence", 0.0),
                model=dispatch_result.model or "sage",
                latency_ms=int((time.time() - t_start) * 1000),
            )

    # Fallback — return evidence summary without LLM reasoning
    log.warning("Sage deep reasoning failed, returning evidence-only fallback")
    evidence_summary = "; ".join(p.content[:100] for p in provenance[:3])
    return DeepReasoningResult(
        answer=f"Could not perform deep reasoning (Sage unavailable). Relevant evidence: {evidence_summary}"
        if evidence_summary
        else "No evidence found and Sage is unavailable.",
        reasoning_steps=[
            "Evidence gathered from memory",
            "LLM reasoning unavailable",
            "Returning raw evidence",
        ],
        provenance=provenance,
        model="heuristic",
        latency_ms=int((time.time() - t_start) * 1000),
    )


def _parse_deep_response(text: str) -> dict[str, Any] | None:
    """Parse Sage's deep reasoning response."""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        extracted = _extract_json_object(text)
        if not extracted:
            return None
        try:
            data = json.loads(extracted)
        except json.JSONDecodeError:
            return None

    answer = data.get("answer", "")
    steps = data.get("steps", [])
    if not answer:
        return None
    if not isinstance(steps, list):
        steps = [str(steps)]
    confidence = 0.0
    try:
        confidence = min(1.0, max(0.0, float(data.get("confidence", 0.0))))
    except (ValueError, TypeError):
        pass
    return {"answer": answer, "steps": steps, "confidence": confidence}
