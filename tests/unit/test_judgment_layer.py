from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core"))

import judgment_layer


@dataclass
class FakeBlock:
    id: str
    title: str
    content: str
    source: str
    score: float
    priority: str
    path: str | None = None


def test_classify_short_proceed_prompt_disables_memory() -> None:
    judgment = judgment_layer.classify_prompt("좋다 진행하자")

    assert judgment.intent == "execution_control"
    assert judgment.needs_memory is False
    assert judgment.allow_semantic is False
    assert judgment.max_blocks == 0


def test_classify_brain_prompt_allows_memory_even_when_short() -> None:
    judgment = judgment_layer.classify_prompt("브레인 hook 확인해줘")

    assert judgment.needs_memory is True
    assert judgment.allow_semantic is True
    assert judgment.allow_proactive is False
    assert judgment.intent == "implementation"


def test_classify_digest_prompt_allows_interrupt_context() -> None:
    judgment = judgment_layer.classify_prompt("Brain Digest로 온 문제 확인해줘")

    assert judgment.needs_memory is True
    assert judgment.allow_semantic is True
    assert judgment.allow_proactive is True


def test_arbitrate_suppresses_stale_semantic_when_not_requested() -> None:
    judgment = judgment_layer.classify_prompt("내 비용 정책 알려줘")
    stale = FakeBlock(
        id="old",
        title="Old policy",
        content="This memory is deprecated and superseded by a later policy.",
        source="semantic:canonical",
        score=0.99,
        priority="high",
        path="/notes/old.md",
    )
    fresh = FakeBlock(
        id="new",
        title="Current cost policy",
        content="Use GPT and Claude subscription paths; avoid extra API billing.",
        source="semantic:canonical",
        score=0.95,
        priority="high",
        path="/canonical/current.md",
    )

    result = judgment_layer.arbitrate_blocks([stale, fresh], judgment)

    assert result.blocks == [fresh]
    assert result.suppressed["stale_or_superseded"] == 1


def test_arbitrate_allows_critical_push_for_control_prompt() -> None:
    judgment = judgment_layer.classify_prompt("진행해줘")
    critical = FakeBlock(
        id="doorbell",
        title="Urgent",
        content="Critical session alert.",
        source="doorbell:brain_loop",
        score=1.0,
        priority="critical",
    )
    semantic = FakeBlock(
        id="noise",
        title="Noise",
        content="Optional memory.",
        source="semantic:experience",
        score=0.99,
        priority="high",
    )

    result = judgment_layer.arbitrate_blocks([semantic, critical], judgment)

    assert result.blocks == [critical]
    assert result.suppressed["memory_not_needed"] == 1


def test_arbitrate_keeps_stale_candidate_when_prompt_requests_stale_review() -> None:
    judgment = judgment_layer.classify_prompt("stale 된 기억 확인해줘")
    stale = FakeBlock(
        id="old",
        title="Old policy",
        content="This memory is deprecated and superseded by a later policy.",
        source="semantic:knowledge",
        score=0.99,
        priority="high",
        path="/notes/old.md",
    )

    result = judgment_layer.arbitrate_blocks([stale], judgment)

    assert result.blocks == [stale]


def test_arbitrate_prefers_canonical_over_semantic_duplicate() -> None:
    judgment = judgment_layer.classify_prompt("브레인 비용 정책")
    semantic = FakeBlock(
        id="sem",
        title="Cost policy",
        content="Use GPT and Claude subscription paths; avoid extra API billing.",
        source="semantic:experience",
        score=0.99,
        priority="high",
    )
    canonical = FakeBlock(
        id="can",
        title="Cost policy",
        content="Use GPT and Claude subscription paths; avoid extra API billing.",
        source="canonical",
        score=1.0,
        priority="high",
        path="/canonical/cost.md",
    )

    result = judgment_layer.arbitrate_blocks([semantic, canonical], judgment)

    assert result.blocks == [canonical]
    assert result.suppressed["near_duplicate"] == 1
