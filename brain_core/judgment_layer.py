"""Lightweight judgment policy for active recall.

This module is intentionally deterministic and dependency-free. It sits inside
the existing /recall/active path and decides whether retrieved memories should
enter the prompt at all. The goal is not better search; it is better judgment
about when search evidence is useful, stale, or noisy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol


class RecallBlock(Protocol):
    id: str
    title: str
    content: str
    source: str
    score: float
    priority: str
    path: str | None


@dataclass(frozen=True)
class PromptJudgment:
    intent: str
    needs_memory: bool
    allow_semantic: bool
    allow_proactive: bool
    max_blocks: int
    max_tokens: int
    min_semantic_score: float
    reason: str
    asks_about_stale: bool = False

    def to_dict(self) -> dict:
        return {
            "intent": self.intent,
            "needs_memory": self.needs_memory,
            "allow_semantic": self.allow_semantic,
            "allow_proactive": self.allow_proactive,
            "max_blocks": self.max_blocks,
            "max_tokens": self.max_tokens,
            "min_semantic_score": self.min_semantic_score,
            "reason": self.reason,
            "asks_about_stale": self.asks_about_stale,
        }


@dataclass
class ArbitrationResult:
    blocks: list[RecallBlock]
    suppressed: dict[str, int] = field(default_factory=dict)

    def to_quality_dict(self) -> dict:
        return {"suppressed": dict(sorted(self.suppressed.items()))}


_CONTROL_RE = re.compile(
    r"^\s*(?:"
    r"좋다|오케이|오케|ㅇㅋ|응|그래|진행|진행해|진행해줘|계속|계속해|계속 진행|"
    r"시작|하자|해줘|그렇게 하자|"
    r"ok|okay|yes|yep|go ahead|proceed|continue|do it|let'?s do it|start"
    r")[\s.!?。…]*$",
    re.IGNORECASE,
)

_DOMAIN_TERMS = {
    "brain",
    "브레인",
    "뇌",
    "recall",
    "active recall",
    "hook",
    "prehook",
    "userpromptsubmit",
    "codex",
    "claude",
    "openclaw",
    "mcp",
    "skill",
    "self-learning",
    "stale",
    "canonical",
    "memory",
    "메모리",
    "기억",
    "scheduler",
    "eval",
    "pipeline",
    "cost",
    "subscription",
    "resource",
    "design",
    "frontend",
    "ui",
    "ux",
    "docker",
    "cloudflare",
    "launchd",
    "리소스",
    "비용",
    "구독",
    "디자인",
    "프론트엔드",
    "프론트",
}

_QUESTION_MARKERS = (
    "?",
    "뭐",
    "어떻게",
    "왜",
    "언제",
    "무엇",
    "맞아",
    "맞나",
    "알려",
    "확인",
    "review",
    "analyze",
    "what",
    "why",
    "how",
    "when",
    "which",
)

_CODE_MARKERS = (
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".sh",
    "test",
    "pytest",
    "ruff",
    "commit",
    "push",
    "hook",
    "prehook",
    "코드",
    "테스트",
    "커밋",
    "푸쉬",
    "수정",
    "구현",
)

_PERSONAL_MARKERS = (
    "내가",
    "내 ",
    "나의",
    "my ",
    "i prefer",
    "preference",
    "선호",
    "원하는",
    "방향",
    "정책",
)

_STALE_MARKERS = (
    "stale",
    "outdated",
    "obsolete",
    "superseded",
    "deprecated",
    "archive",
    "archived",
    "오래된",
    "낡은",
    "폐기",
    "대체",
)


def classify_prompt(prompt: str | None, *, cwd: str | None = None) -> PromptJudgment:
    """Classify prompt shape before active recall spends context budget."""

    _ = cwd  # Reserved for future project-local policies; do not trigger recall from cwd alone.
    text = (prompt or "").strip()
    lowered = text.lower()
    asks_about_stale = _contains_any(lowered, _STALE_MARKERS)
    has_domain = _contains_any(lowered, _DOMAIN_TERMS)
    is_question = _contains_any(lowered, _QUESTION_MARKERS)

    if not text:
        return PromptJudgment(
            intent="empty",
            needs_memory=False,
            allow_semantic=False,
            allow_proactive=False,
            max_blocks=0,
            max_tokens=0,
            min_semantic_score=1.0,
            reason="empty prompt",
        )

    if (_CONTROL_RE.match(text) or _is_control_phrase(text)) and not has_domain and len(text) <= 80:
        return PromptJudgment(
            intent="execution_control",
            needs_memory=False,
            allow_semantic=False,
            allow_proactive=False,
            max_blocks=0,
            max_tokens=0,
            min_semantic_score=1.0,
            reason="short proceed/ack prompt; memory would be hook noise",
        )

    if _contains_any(lowered, _CODE_MARKERS) or _looks_like_path_or_command(text):
        return PromptJudgment(
            intent="implementation",
            needs_memory=True,
            allow_semantic=True,
            allow_proactive=True,
            max_blocks=4,
            max_tokens=1400,
            min_semantic_score=0.78,
            reason="implementation prompt benefits from project policy and recent lessons",
            asks_about_stale=asks_about_stale,
        )

    if _contains_any(lowered, _PERSONAL_MARKERS) or has_domain:
        return PromptJudgment(
            intent="policy_or_memory",
            needs_memory=True,
            allow_semantic=True,
            allow_proactive=True,
            max_blocks=5,
            max_tokens=1600,
            min_semantic_score=0.76,
            reason="prompt references durable preference, policy, or brain domain",
            asks_about_stale=asks_about_stale,
        )

    if is_question and len(text) >= 20:
        return PromptJudgment(
            intent="factual_question",
            needs_memory=True,
            allow_semantic=True,
            allow_proactive=False,
            max_blocks=3,
            max_tokens=1000,
            min_semantic_score=0.84,
            reason="question may need memory but should use a conservative budget",
            asks_about_stale=asks_about_stale,
        )

    return PromptJudgment(
        intent="generic",
        needs_memory=False,
        allow_semantic=False,
        allow_proactive=False,
        max_blocks=0,
        max_tokens=0,
        min_semantic_score=1.0,
        reason="generic short prompt; no reliable evidence need detected",
        asks_about_stale=asks_about_stale,
    )


def arbitrate_blocks(blocks: list[RecallBlock], judgment: PromptJudgment) -> ArbitrationResult:
    """Filter and order active recall blocks by relevance authority."""

    counters: dict[str, int] = {}
    if not judgment.needs_memory:
        kept = [b for b in blocks if _is_critical_push(b)]
        _count(counters, "memory_not_needed", len(blocks) - len(kept))
        return ArbitrationResult(blocks=kept[: max(1, judgment.max_blocks)], suppressed=counters)

    selected: list[RecallBlock] = []
    seen_signatures: list[set[str]] = []
    for block in sorted(blocks, key=_block_sort_key):
        if block.source.startswith("semantic") and block.score < judgment.min_semantic_score:
            _count(counters, "below_intent_score")
            continue
        if _is_stale_block(block) and not judgment.asks_about_stale and not _is_canonical(block):
            _count(counters, "stale_or_superseded")
            continue
        signature = _signature(f"{block.title}\n{block.content[:800]}")
        if signature and any(_jaccard(signature, prior) >= 0.68 for prior in seen_signatures):
            _count(counters, "near_duplicate")
            continue
        selected.append(block)
        if signature:
            seen_signatures.append(signature)
        if len(selected) >= judgment.max_blocks:
            break

    overflow = max(0, len(blocks) - len(selected) - sum(counters.values()))
    _count(counters, "over_budget", overflow)
    return ArbitrationResult(blocks=selected, suppressed=counters)


def _block_sort_key(block: RecallBlock) -> tuple[int, int, float]:
    priority_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(block.priority, 9)
    authority_rank = _authority_rank(block)
    return (priority_rank, authority_rank, -float(block.score or 0.0))


def _authority_rank(block: RecallBlock) -> int:
    source = (block.source or "").lower()
    path = (block.path or "").lower()
    if source == "canonical":
        return 0
    if source.startswith("doorbell"):
        return 1
    if "canonical" in source or "/canonical/" in path:
        return 2
    if source.startswith("proactive"):
        return 3
    if "semantic" in source:
        return 4
    return 5


def _is_canonical(block: RecallBlock) -> bool:
    return (block.source or "").lower() == "canonical" or "/canonical/" in (block.path or "").lower()


def _is_critical_push(block: RecallBlock) -> bool:
    return block.priority == "critical" or (block.source or "").startswith("doorbell")


def _is_stale_block(block: RecallBlock) -> bool:
    haystack = f"{block.title}\n{block.path or ''}\n{block.content[:500]}".lower()
    return _contains_any(haystack, _STALE_MARKERS)


def _contains_any(text: str, markers: tuple[str, ...] | set[str]) -> bool:
    return any(marker in text for marker in markers)


def _looks_like_path_or_command(text: str) -> bool:
    lowered = text.lower()
    return bool(
        re.search(r"(?:^|\s)(?:git|npm|pnpm|pytest|ruff|curl|python|uv|docker)\s+", lowered)
        or re.search(r"(?:^|/)[\w.-]+/(?:[\w.-]+/)*[\w.-]+\.[a-z0-9]{1,8}\b", lowered)
    )


def _is_control_phrase(text: str) -> bool:
    return bool(
        re.match(
            r"^\s*(?:좋다|오케이|오케|응|그래)[\s,]*(?:진행|계속|하자|해줘|시작).{0,20}$",
            text,
            re.IGNORECASE,
        )
    )


def _signature(text: str) -> set[str]:
    cleaned = re.sub(r"\b20\d{2}-\d{2}-\d{2}\b", " ", text or "")
    return {tok for tok in re.findall(r"[a-zA-Z가-힣0-9]{3,}", cleaned.lower()) if len(tok) >= 3}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, len(a | b))


def _count(counters: dict[str, int], key: str, amount: int = 1) -> None:
    if amount <= 0:
        return
    counters[key] = counters.get(key, 0) + amount
