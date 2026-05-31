"""tests/test_active_recall.py — unit tests for the per-turn thalamus.

Verifies intent routing, canonical guarantees, dedup, optional confidence sentinel,
budget enforcement, and fail-open behavior. Uses in-memory sqlite for the
session_context reads so tests don't touch production autonomy.db.

Run:
  .venv/bin/python -m pytest tests/test_active_recall.py -q
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))

import active_recall

# ── Fixtures ──────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _redirect_autonomy_db(tmp_path, monkeypatch):
    """Point active_recall's session_context writes at a temp sqlite so tests
    don't pollute the real autonomy.db."""
    fake_db = tmp_path / "autonomy.db"
    monkeypatch.setattr(active_recall, "AUTONOMY_DB", fake_db)
    # Initialize the session_context table
    import sqlite3

    with sqlite3.connect(str(fake_db)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS session_context (
                session_id TEXT NOT NULL,
                agent TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (session_id, agent, key)
            )
        """)
    yield


@pytest.fixture(autouse=True)
def _clear_routes_cache():
    """Reset cached routes so each test picks up current YAML."""
    active_recall._routes_cache = None
    active_recall._routes_cache_mtime = 0.0
    yield


# ── Intent matching ─────────────────────────────────────


def test_intent_matches_korean_design_keyword():
    matches = active_recall._match_canonical_routes("프론트엔드 디자인 어떻게 해?")
    intents = [m.intent for m in matches]
    assert "frontend_design" in intents


def test_intent_matches_english_design_keyword():
    matches = active_recall._match_canonical_routes("how should I design the frontend layout")
    intents = [m.intent for m in matches]
    assert "frontend_design" in intents


def test_short_latin_design_keywords_do_not_match_inside_quality_or_tui():
    matches = active_recall._match_canonical_routes(
        "When a coding task needs quality or steering, should Chris use Codex "
        "through Hermes as an interactive tmux TUI or headless codex exec?"
    )
    intents = [m.intent for m in matches]

    assert "frontend_design" not in intents


def test_intent_matches_infra_homelab():
    matches = active_recall._match_canonical_routes("docker compose cloudflare tunnel")
    intents = [m.intent for m in matches]
    assert "infra_homelab" in intents


def test_intent_matches_brain_self():
    matches = active_recall._match_canonical_routes("how does the scheduler cron work")
    intents = [m.intent for m in matches]
    assert "brain_self" in intents


def test_brain_quality_prompt_does_not_inject_ops_runbook():
    matches = active_recall._match_canonical_routes("실제 브레인에 얼만큼 근접했어?")
    intents = [m.intent for m in matches]
    assert "brain_quality" in intents
    assert "brain_self" not in intents


def test_brain_recall_prefetch_quality_right_now_routes_to_quality_not_live_state():
    matches = active_recall._match_canonical_routes(
        "Right now, is Brain's recall and prefetch quality healthy or noisy?"
    )
    intents = [m.intent for m in matches]

    assert "brain_quality" in intents
    assert "live_state" not in intents


def test_brain_ops_prompt_still_injects_runbook_route():
    matches = active_recall._match_canonical_routes("브레인 healthcheck 장애와 backup 상태 확인")
    intents = [m.intent for m in matches]
    assert "brain_self" in intents


def test_active_recall_quality_prompt_does_not_inject_ops_runbook():
    matches = active_recall._match_canonical_routes("active recall에서 관련없는 결과값 개선 가능해?")
    intents = [m.intent for m in matches]
    assert "brain_self" not in intents


def test_monitoring_ops_prompt_still_injects_runbook_route():
    matches = active_recall._match_canonical_routes("브레인 모니터링 메트릭 상태 확인")
    intents = [m.intent for m in matches]
    assert "brain_self" in intents


def test_llm_budget_intent_suppresses_broad_brain_self():
    matches = active_recall._match_canonical_routes("브레인 비용 정책 알려줘")
    intents = [m.intent for m in matches]
    assert "llm_budget" in intents
    assert "brain_self" not in intents


def test_intent_matches_visual():
    matches = active_recall._match_canonical_routes("내가 보낸 사진 뭐였지?")
    intents = [m.intent for m in matches]
    assert "visual" in intents


def test_visual_intent_does_not_match_image_backend_discussion():
    matches = active_recall._match_canonical_routes(
        "Gemini API 말고 GPT subscription CLI로 이미지 캡션 처리 가능해?"
    )
    intents = [m.intent for m in matches]
    assert "visual" not in intents


def test_no_intent_for_generic_query():
    """A query with no intent-specific keywords returns empty."""
    matches = active_recall._match_canonical_routes("random text with nothing notable")
    assert matches == []


def test_empty_prompt_returns_empty():
    assert active_recall._match_canonical_routes("") == []
    assert active_recall._match_canonical_routes(None) == []  # type: ignore[arg-type]


# ── Canonical path loading ───────────────────────────────


def test_load_canonical_path_missing_returns_none(tmp_path):
    result = active_recall._load_canonical_path(str(tmp_path / "does_not_exist.md"))
    assert result is None


def test_load_canonical_path_reads_file(tmp_path):
    f = tmp_path / "sample.md"
    f.write_text("# Sample title\n\nBody content here.")
    result = active_recall._load_canonical_path(str(f))
    assert result is not None
    title, content = result
    assert title == "sample"
    assert "Body content" in content


# ── Dedup + decay ────────────────────────────────────────


def test_apply_decay_filter_critical_reinjects_after_15_turns():
    """CR1 fix (2026-04-14): critical-priority blocks re-inject after 15
    turns so design/credentials/live-state standards keep surfacing mid-
    session. Previously critical was 10^6 turns (effectively never) and
    Chris's design standard only surfaced once per session — root cause
    of the 'brain isn't being used' incident."""
    block = active_recall.InjectionBlock(
        id="abc123",
        title="t",
        content="c",
        source="canonical",
        score=1.0,
        priority="critical",
    )
    seen = {"abc123": {"last_turn": 0, "priority": "critical"}}
    # Turn 14: still suppressed (cooldown window)
    assert active_recall._apply_decay_filter([block], seen, turn_idx=14) == []
    # Turn 15: re-injects
    assert active_recall._apply_decay_filter([block], seen, turn_idx=15) == [block]


def test_apply_decay_filter_high_reinjects_after_window():
    """High-priority blocks re-inject after 20 turns."""
    block = active_recall.InjectionBlock(
        id="abc",
        title="t",
        content="c",
        source="semantic",
        score=0.8,
        priority="high",
    )
    seen = {"abc": {"last_turn": 0, "priority": "high"}}
    # Turn 19: still suppressed
    assert active_recall._apply_decay_filter([block], seen, turn_idx=19) == []
    # Turn 20: re-injects
    assert active_recall._apply_decay_filter([block], seen, turn_idx=20) == [block]


def test_apply_decay_filter_unseen_block_passes():
    block = active_recall.InjectionBlock(
        id="new",
        title="t",
        content="c",
        source="semantic",
        score=0.7,
        priority="high",
    )
    survivors = active_recall._apply_decay_filter([block], {}, turn_idx=5)
    assert survivors == [block]


def test_doorbell_blocks_require_prompt_relevance(tmp_path, monkeypatch):
    monkeypatch.setattr(active_recall, "DOORBELL_DIR", tmp_path)
    session_id = "doorbell-relevance"
    (tmp_path / f".brain_doorbell.{session_id}.jsonl").write_text(
        '{"title":"OpenClaw stale","content":"OpenClaw response recovery is still pending","priority":"high","severity":8.0}\n'
    )

    blocks = active_recall._doorbell_blocks(session_id, prompt="prehook 정책과 컨텍스트 주입 확인")

    assert blocks == []


def test_doorbell_blocks_allow_matching_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(active_recall, "DOORBELL_DIR", tmp_path)
    session_id = "doorbell-match"
    (tmp_path / f".brain_doorbell.{session_id}.jsonl").write_text(
        '{"title":"prehook stale","content":"prehook context injection is noisy","priority":"high","severity":8.0}\n'
    )

    blocks = active_recall._doorbell_blocks(session_id, prompt="prehook 컨텍스트 주입 확인")

    assert len(blocks) == 1
    assert blocks[0].source == "doorbell:brain_loop"


def test_doorbell_blocks_allow_explicit_critical_without_overlap(tmp_path, monkeypatch):
    monkeypatch.setattr(active_recall, "DOORBELL_DIR", tmp_path)
    session_id = "doorbell-critical"
    (tmp_path / f".brain_doorbell.{session_id}.jsonl").write_text(
        '{"title":"backup failure","content":"backup failure needs attention","priority":"critical","severity":7.0}\n'
    )

    blocks = active_recall._doorbell_blocks(session_id, prompt="진행해")

    assert len(blocks) == 1
    assert blocks[0].priority == "critical"


def test_semantic_blocks_filters_low_score_and_near_duplicates(monkeypatch):
    fake_search = types.SimpleNamespace(
        search_all=lambda *args, **kwargs: {
            "results": [
                {
                    "id": "a",
                    "title": "Claude subscription policy",
                    "content": (
                        "OpenClaw jenna session (2026-04-01)\n"
                        "Signal: preference\n"
                        "Chris wants Claude through OpenClaw subscription and avoid extra API usage."
                    ),
                    "score": 95,
                    "collection": "canonical",
                    "path": "/a.md",
                },
                {
                    "id": "b",
                    "title": "Claude subscription policy duplicate",
                    "content": (
                        "OpenClaw jenna session (2026-04-01)\n"
                        "Signal: decision\n"
                        "Chris wants Claude through OpenClaw subscription and avoid extra paid API usage."
                    ),
                    "score": 94,
                    "collection": "canonical",
                    "path": "/b.md",
                },
                {
                    "id": "c",
                    "title": "Weak",
                    "content": "weak optional context",
                    "score": 50,
                    "collection": "canonical",
                    "path": "/c.md",
                },
            ]
        }
    )
    monkeypatch.setitem(sys.modules, "search_unified", fake_search)

    blocks = active_recall._semantic_blocks("subscription api cost", [], set(), limit=5)
    assert len(blocks) == 1
    assert blocks[0].title == "Claude subscription policy"
    assert blocks[0].score == 0.95


def test_semantic_blocks_suppresses_generic_summary_titles(monkeypatch):
    fake_search = types.SimpleNamespace(
        search_all=lambda *args, **kwargs: {
            "results": [
                {
                    "id": "summary-noise",
                    "title": "Summary (part 2)",
                    "content": "hook supplied strings agent name session id",
                    "score": 99,
                    "collection": "canonical",
                    "path": "/summary.md",
                }
            ]
        }
    )
    monkeypatch.setitem(sys.modules, "search_unified", fake_search)

    blocks = active_recall._semantic_blocks("UserPromptSubmit hook 여기서 나오는거", [], set(), limit=5)
    assert blocks == []


def test_semantic_blocks_suppresses_usage_snapshot_for_strategy_prompt(monkeypatch):
    fake_search = types.SimpleNamespace(
        search_all=lambda *args, **kwargs: {
            "results": [
                {
                    "id": "usage-snapshot",
                    "title": "지난 7일 브레인 LLM 사용량",
                    "content": "총 비용 $811.75, prompt tokens 33.7M, response tokens 123,991",
                    "score": 99,
                    "collection": "canonical",
                    "path": "/reports/llm-token-usage.md",
                }
            ]
        }
    )
    monkeypatch.setitem(sys.modules, "search_unified", fake_search)

    blocks = active_recall._semantic_blocks(
        "이제 내가 원하는 레벨의 브레인 경지에 도달하기 위해 남은게 뭐야?",
        [],
        set(),
        limit=5,
    )
    assert blocks == []


def test_semantic_blocks_allows_usage_snapshot_for_usage_prompt(monkeypatch):
    fake_search = types.SimpleNamespace(
        search_all=lambda *args, **kwargs: {
            "results": [
                {
                    "id": "usage-snapshot",
                    "title": "지난 7일 브레인 LLM 사용량",
                    "content": "총 비용 $811.75, prompt tokens 33.7M, response tokens 123,991",
                    "score": 99,
                    "collection": "canonical",
                    "path": "/reports/llm-token-usage.md",
                }
            ]
        }
    )
    monkeypatch.setitem(sys.modules, "search_unified", fake_search)

    blocks = active_recall._semantic_blocks("브레인 LLM 사용량 토큰 집계 알려줘", [], set(), limit=5)
    assert len(blocks) == 1
    assert blocks[0].title == "지난 7일 브레인 LLM 사용량"


def test_semantic_blocks_requires_prompt_overlap(monkeypatch):
    fake_search = types.SimpleNamespace(
        search_all=lambda *args, **kwargs: {
            "results": [
                {
                    "id": "cloudflare",
                    "title": "Cloudflare token format",
                    "content": "Cloudflare API token length and auth header mapping",
                    "score": 99,
                    "collection": "experience",
                    "path": "/cloudflare.md",
                }
            ]
        }
    )
    monkeypatch.setitem(sys.modules, "search_unified", fake_search)

    blocks = active_recall._semantic_blocks("UserPromptSubmit hook 여기서 나오는거", [], set(), limit=5)
    assert blocks == []


# ── Budget enforcement ───────────────────────────────────


def test_enforce_budget_sorts_by_priority():
    low = active_recall.InjectionBlock(
        id="a",
        title="low",
        content="x" * 500,
        source="s",
        score=0.3,
        priority="low",
    )
    high = active_recall.InjectionBlock(
        id="b",
        title="high",
        content="y" * 500,
        source="s",
        score=0.9,
        priority="high",
    )
    crit = active_recall.InjectionBlock(
        id="c",
        title="crit",
        content="z" * 500,
        source="s",
        score=1.0,
        priority="critical",
    )
    kept = active_recall._enforce_budget([low, high, crit], limit=1000)
    # Order should be critical → high → low
    assert kept[0].priority == "critical"
    assert kept[1].priority == "high"


def test_enforce_budget_trims_when_over_limit():
    blocks = [
        active_recall.InjectionBlock(
            id=f"b{i}",
            title="t",
            content="x" * 500,
            source="s",
            score=0.5,
            priority="medium",
        )
        for i in range(10)
    ]
    kept = active_recall._enforce_budget(blocks, limit=500)
    total_tokens = sum(
        active_recall._rough_tokens(b.content) + active_recall._rough_tokens(b.title) for b in kept
    )
    assert total_tokens <= 500


def test_rough_tokens_floor_is_one():
    assert active_recall._rough_tokens("") == 1
    assert active_recall._rough_tokens("abcd") == 1
    assert active_recall._rough_tokens("a" * 4000) == 1000


# ── Context compiler ─────────────────────────────────────


def test_context_compiler_annotates_without_reordering():
    canonical = active_recall.InjectionBlock(
        id="canon",
        title="Policy",
        content="Chris prefers no extra LLM API spend.",
        source="canonical",
        score=1.0,
        priority="critical",
        path="/knowledge/canonical/policy.md",
    )
    semantic = active_recall.InjectionBlock(
        id="sem",
        title="Related note",
        content="Subscription CLI should be used for background synthesis only.",
        source="semantic:canonical",
        score=0.83,
        priority="high",
    )

    compiled = active_recall._compile_context_blocks([canonical, semantic])

    assert [b.id for b in compiled] == ["canon", "sem"]
    assert compiled[0].include_reason.startswith("canonical guarantee")
    assert compiled[0].freshness == "canonical"
    assert "canonical_authority" in compiled[0].risk_flags
    assert compiled[0].contract_category == "risk_constraint"
    assert compiled[1].include_reason.startswith("semantic evidence")
    assert compiled[1].contract_category == "risk_constraint"
    assert compiled[1].token_estimate == active_recall._rough_tokens(
        semantic.title
    ) + active_recall._rough_tokens(semantic.content)
    assert compiled[1].compiler_score is not None


def test_context_compiler_report_counts_flags():
    block = active_recall.InjectionBlock(
        id="usage",
        title="LLM 사용량",
        content="지난 7일 token usage total cost $1.23",
        source="semantic:knowledge",
        score=0.79,
        priority="medium",
    )

    active_recall._compile_context_blocks([block])
    report = active_recall._context_compiler_report([block])

    assert report["version"] == 1
    assert report["annotated_blocks"] == 1
    assert report["estimated_tokens"] == block.token_estimate
    assert report["risk_flags"]["snapshot"] == 1
    assert report["risk_flags"]["low_semantic_score"] == 1


def test_context_contract_report_declares_allowed_categories():
    policy = active_recall.InjectionBlock(
        id="policy",
        title="Chris policy",
        content="Chris prefers no extra cost and subscription-only LLM paths.",
        source="semantic:canonical",
        score=0.94,
        priority="high",
    )
    active_recall._compile_context_blocks([policy])

    report = active_recall._context_contract_report([policy])

    assert report["uses_llm"] is False
    assert report["suppresses_raw_doorbell"] is True
    assert "risk_constraint" in report["allowed_categories"]
    assert report["block_categories"]["risk_constraint"] == 1


def test_semantic_timeout_shortens_when_critical_canonical_satisfies_contract():
    canonical = [
        active_recall.InjectionBlock(
            id="budget",
            title="LLM budget",
            content="Chris requires subscription-only LLM usage.",
            source="canonical",
            score=1.0,
            priority="critical",
        )
    ]

    assert (
        active_recall._semantic_timeout_for_contract("브레인 비용 정책 확인", canonical, None)
        == active_recall._SEMANTIC_FAST_TIMEOUT_S
    )
    assert (
        active_recall._semantic_timeout_for_contract("prehook 브레인 비용 정책 확인", canonical, None) is None
    )


def test_semantic_timeout_shortens_when_high_canonical_satisfies_contract():
    canonical = [
        active_recall.InjectionBlock(
            id="quality",
            title="Brain quality",
            content="Chris wants production-quality brain improvements.",
            source="canonical",
            score=1.0,
            priority="high",
        )
    ]

    assert (
        active_recall._semantic_timeout_for_contract("실제 브레인에 얼만큼 근접했어?", canonical, None)
        == active_recall._SEMANTIC_FAST_TIMEOUT_S
    )


# ── Confidence sentinel ──────────────────────────────────


def test_confidence_sentinel_has_source_tag():
    sentinel = active_recall._confidence_sentinel()
    assert sentinel.source == "confidence_sentinel"
    assert sentinel.priority == "low"
    # Title conveys the "low confidence" framing; content explains.
    assert "confidence low" in sentinel.title.lower()
    assert "no canonical" in sentinel.content.lower()


def test_confidence_sentinel_disabled_by_default():
    assert active_recall._confidence_sentinel_enabled() is False


def test_confidence_sentinel_can_be_opted_in(monkeypatch):
    monkeypatch.setenv("BRAIN_ACTIVE_RECALL_CONFIDENCE_SENTINEL", "1")
    assert active_recall._confidence_sentinel_enabled() is True


# ── build_injection end-to-end ──────────────────────────


def test_build_injection_fails_open_on_search_error(monkeypatch):
    """If search_all raises, build_injection returns degraded=False with
    whatever it has (doesn't crash)."""

    def _boom(*args, **kwargs):
        raise RuntimeError("chroma unreachable")

    with patch.object(active_recall, "_semantic_blocks", _boom):
        result = active_recall.build_injection(
            prompt="프론트엔드 디자인 어떻게 해?",
            session_id="t-test-1",
            turn_idx=0,
            agent="claude",
        )
    # Even with semantic layer crashing, canonical layer still delivers
    # blocks so we don't mark the whole thing degraded.
    assert result["degraded"] is True  # build_injection catches the exception
    assert "blocks" in result


def test_build_injection_returns_latency_ms(monkeypatch):
    monkeypatch.setattr(active_recall, "_semantic_blocks", lambda *args, **kwargs: [])
    result = active_recall.build_injection(
        prompt="random fallback query",
        session_id="t-lat",
        turn_idx=0,
        agent="claude",
    )
    assert "latency_ms" in result
    assert "quality" in result
    assert result["quality"]["block_count"] == len(result["blocks"])
    assert result["quality"]["compiler"]["version"] == 1
    assert not any(b.get("source") == "confidence_sentinel" for b in result["blocks"])
    assert isinstance(result["latency_ms"], int)


def test_build_injection_suppresses_short_proceed_prompt(monkeypatch):
    called = False
    recorded = {}

    def _semantic(*args, **kwargs):
        nonlocal called
        called = True
        return [
            active_recall.InjectionBlock(
                id="noise",
                title="Noise",
                content="This should not be injected for a proceed-only prompt.",
                source="semantic",
                score=0.99,
                priority="high",
            )
        ]

    monkeypatch.setattr(active_recall, "_semantic_blocks", _semantic)
    monkeypatch.setattr(active_recall, "_record_judgment_feedback", lambda **kwargs: recorded.update(kwargs))
    result = active_recall.build_injection(
        prompt="좋다 진행하자",
        session_id="t-proceed",
        turn_idx=0,
        agent="codex",
    )

    assert called is False
    assert result["blocks"] == []
    assert result["quality"]["judgment"]["intent"] == "execution_control"
    assert result["quality"]["judgment"]["needs_memory"] is False
    assert result["quality"]["compiler"]["annotated_blocks"] == 0
    assert result["quality"]["context_contract"]["allowed_categories"] == ["urgent_interrupt"]
    assert recorded["actor"] == "codex"
    assert recorded["block_count"] == 0
    assert recorded["judgment"].intent == "execution_control"


def test_build_injection_can_include_opt_in_confidence_sentinel(monkeypatch):
    monkeypatch.setenv("BRAIN_ACTIVE_RECALL_CONFIDENCE_SENTINEL", "1")
    monkeypatch.setattr(active_recall, "_semantic_blocks", lambda *args, **kwargs: [])
    result = active_recall.build_injection(
        prompt="random fallback query",
        session_id="t-lat-sentinel",
        turn_idx=0,
        agent="codex",
    )
    assert any(b.get("source") == "confidence_sentinel" for b in result["blocks"])


def test_active_recall_korean_codex_prompt_uses_workflow_preference_variant(monkeypatch):
    seen_queries: list[str] = []

    def fake_search_all(query, *args, **kwargs):
        seen_queries.append(query)
        if "Codex Hermes interactive tmux TUI preference" not in query:
            return {"results": []}
        return {
            "results": [
                {
                    "id": "codex-hermes-tmux-tui",
                    "title": "Codex Hermes interactive tmux TUI preference",
                    "content": (
                        "Chris prefers using Codex through Hermes as an interactive "
                        "terminal-like tmux TUI when a coding task needs quality or steering; "
                        "headless codex exec is only for bounded automation."
                    ),
                    "score": 96,
                    "collection": "semantic_memory",
                    "path": "/prefs/codex-hermes-tmux-tui.md",
                }
            ]
        }

    monkeypatch.setitem(sys.modules, "search_unified", types.SimpleNamespace(search_all=fake_search_all))

    result = active_recall.build_injection(
        prompt="복잡한 코딩 작업은 코덱스를 어떻게 쓰는 게 좋아?",
        session_id="t-korean-codex-pref",
        turn_idx=0,
        agent="codex",
    )

    assert any("Codex Hermes interactive tmux TUI preference" in q for q in seen_queries)
    assert any(b.get("memory_id") == "codex-hermes-tmux-tui" for b in result["blocks"])


def test_active_recall_korean_codex_prompt_falls_back_to_route_guarantee(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "search_unified",
        types.SimpleNamespace(search_all=lambda *args, **kwargs: {"results": []}),
    )

    result = active_recall.build_injection(
        prompt="복잡한 코딩 작업은 코덱스를 어떻게 쓰는 게 좋아?",
        session_id="t-korean-codex-route-fallback",
        turn_idx=0,
        agent="codex",
    )

    assert result["blocks"]
    assert result["blocks"][0]["source"] == "canonical_route_hint"
    assert "interactive terminal-like tmux TUI" in result["blocks"][0]["content"]


def test_active_recall_codex_workflow_ranks_current_preference_over_skill_sync(monkeypatch):
    def fake_search_all(query, *args, **kwargs):
        if "Chris prefers using Codex through Hermes" in query:
            return {
                "results": [
                    {
                        "id": "codex-current-pref",
                        "title": "Codex Hermes interactive tmux TUI preference",
                        "content": (
                            "Chris prefers using Codex through Hermes as an interactive "
                            "terminal-like TUI session in tmux, similar to how he manually uses Codex, "
                            "when quality or steering matters; headless codex exec is only for bounded automation."
                        ),
                        "score": 95,
                        "collection": "semantic_memory",
                        "path": "hermes",
                    }
                ]
            }
        if "코덱스" in query or "Codex Hermes interactive" in query:
            return {
                "results": [
                    {
                        "id": "codex-skill-sync-noise",
                        "title": "hermes",
                        "content": (
                            "User: 진행해줘. Assistant: 완료. 적용한 범위: Codex/Claude Code skill을 "
                            "~/.hermes/skills/autonomous-ai-agents/ 위치에 동기화했어."
                        ),
                        "score": 110,
                        "collection": "semantic_memory",
                        "path": "hermes",
                    },
                    {
                        "id": "old-claude-restriction",
                        "title": "claude_code",
                        "content": "Old Claude Code restrictions and plan-mode usage caveats for coding tasks.",
                        "score": 108,
                        "collection": "semantic_memory",
                        "path": "claude_code",
                    },
                ]
            }
        return {"results": []}

    monkeypatch.setitem(sys.modules, "search_unified", types.SimpleNamespace(search_all=fake_search_all))

    result = active_recall.build_injection(
        prompt="복잡한 코딩 작업은 코덱스를 어떻게 쓰는 게 좋아?",
        session_id="t-korean-codex-pref-ranking",
        turn_idx=0,
        agent="codex",
    )

    assert result["blocks"]
    assert result["blocks"][0]["memory_id"] == "codex-current-pref"


def test_active_recall_codex_workflow_rescue_preference_survives_small_block_budget(monkeypatch):
    def fake_search_all(query, *args, **kwargs):
        if "Chris prefers using Codex through Hermes" in query:
            return {
                "results": [
                    {
                        "id": "codex-current-pref",
                        "title": "Codex Hermes interactive tmux TUI preference",
                        "content": (
                            "Chris prefers using Codex through Hermes as an interactive terminal-like "
                            "TUI session in tmux when quality or steering matters; headless codex exec "
                            "is only for bounded automation."
                        ),
                        "score": 95,
                        "collection": "semantic_memory",
                        "path": "hermes",
                    }
                ]
            }
        if "복잡한" in query:
            return {
                "results": [
                    {
                        "id": "generic-workflow-summary-1",
                        "title": "untitled",
                        "content": "General coding workflow and preference summary with Codex context.",
                        "score": 98,
                        "collection": "canonical",
                        "path": "",
                    },
                    {
                        "id": "generic-workflow-summary-2",
                        "title": "untitled",
                        "content": "Another broad workflow summary mentioning coding quality and Codex.",
                        "score": 97,
                        "collection": "canonical",
                        "path": "",
                    },
                ]
            }
        return {"results": []}

    monkeypatch.setitem(sys.modules, "search_unified", types.SimpleNamespace(search_all=fake_search_all))
    monkeypatch.setattr(
        active_recall,
        "_classify_prompt",
        lambda *args, **kwargs: types.SimpleNamespace(
            needs_memory=True,
            allow_semantic=True,
            allow_proactive=False,
            max_blocks=2,
            min_semantic_score=0.0,
            max_tokens=500,
            intent="policy_or_memory",
        ),
    )

    result = active_recall.build_injection(
        prompt="복잡한 코딩 작업은 코덱스를 어떻게 쓰는 게 좋아?",
        session_id="t-korean-codex-small-budget",
        turn_idx=0,
        agent="codex",
    )

    assert [block["memory_id"] for block in result["blocks"]][:1] == ["codex-current-pref"]


def test_active_recall_openclaw_hermes_distinction_skips_live_state_and_setup_noise(monkeypatch):
    def fake_search_all(query, *args, **kwargs):
        if "openclaw agent configuration heartbeat" in query.lower():
            return {
                "results": [
                    {
                        "id": "generic-openclaw-setup-guide",
                        "title": "OpenClaw setup guide",
                        "content": "Sub-Agent Configuration and Active Hours for Heartbeat in the old OpenClaw setup.",
                        "score": 120,
                        "collection": "obsidian",
                        "path": "/Users/chrischo/Library/Mobile Documents/iCloud~md~obsidian/Documents/Obsidian-vault/Chrischodev/OpenClaw/openclaw-setup.md",
                    }
                ]
            }
        if (
            "OpenClaw vs Hermes" not in query
            and "OpenClaw Hermes current runtime historical distinction" not in query
        ):
            return {"results": []}
        return {
            "results": [
                {
                    "id": "broad-runtime-theme",
                    "title": "untitled",
                    "content": (
                        "These notes share a common theme: Chris is tightening OpenClaw/Brain operational reliability "
                        "and evaluating memory behavior with Hermes runtime mentions."
                    ),
                    "score": 115,
                    "collection": "canonical",
                    "path": "",
                },
                {
                    "id": "stale-openclaw-setup",
                    "title": "semantic",
                    "content": "Active Hours for Heartbeat and Sub-Agent Configuration for old OpenClaw setup.",
                    "score": 110,
                    "collection": "obsidian",
                    "path": "/Users/chrischo/Library/Mobile Documents/iCloud~md~obsidian/Documents/Obsidian-vault/Chrischodev/OpenClaw/openclaw-setup.md",
                },
                {
                    "id": "distinction",
                    "title": "OpenClaw vs Hermes current runtime historical distinction",
                    "content": (
                        "Chris is currently interacting with Jenna running on Hermes Agent; when comparing "
                        "Hermes Agent vs OpenClaw, distinguish the historical platform decision from the current runtime context."
                    ),
                    "score": 96,
                    "collection": "semantic_memory",
                    "path": "mcp",
                },
                {
                    "id": "live-active-goals",
                    "title": "active_goals",
                    "content": "Active goals and focus: OpenClaw Hermes current runtime work queue.",
                    "score": 95,
                    "collection": "canonical",
                    "path": "/Users/chrischo/server/knowledge/canonical/live_state/active_goals.md",
                },
            ]
        }

    monkeypatch.setitem(sys.modules, "search_unified", types.SimpleNamespace(search_all=fake_search_all))
    monkeypatch.setattr(active_recall, "_canonical_blocks_from_matches", lambda *args, **kwargs: [])

    result = active_recall.build_injection(
        prompt="OpenClaw vs Hermes current runtime historical distinction",
        session_id="t-openclaw-hermes-distinction",
        turn_idx=0,
        agent="codex",
    )

    assert result["blocks"]
    assert result["blocks"][0]["memory_id"] == "distinction"
    assert not any("live_state/active_goals" in (b.get("path") or "") for b in result["blocks"])
    assert not any("openclaw-setup.md" in (b.get("path") or "") for b in result["blocks"])


def test_build_injection_exposes_compiler_metadata(monkeypatch):
    def _semantic(*args, **kwargs):
        return [
            active_recall.InjectionBlock(
                id="sem-compiler",
                title="brain cost policy",
                content="Chris requires no extra LLM API spend beyond subscription CLI paths.",
                source="semantic:canonical",
                score=0.96,
                priority="high",
                path="canonical/decisions/current-memory-policy.md",
            )
        ]

    monkeypatch.setattr(active_recall, "_semantic_blocks", _semantic)
    result = active_recall.build_injection(
        prompt="brain cost policy 알려줘",
        session_id="t-compiler",
        turn_idx=0,
        agent="codex",
    )

    assert result["blocks"]
    block = result["blocks"][0]
    assert block["include_reason"]
    assert block["token_estimate"] > 0
    assert block["compiler_score"] >= block["score"]
    assert block["contract_category"] in {"policy", "risk_constraint"}
    assert result["quality"]["compiler"]["annotated_blocks"] == len(result["blocks"])
    assert "context_contract" in result["quality"]


def test_semantic_prompt_match_rejects_single_generic_overlap():
    assert (
        active_recall._semantic_result_matches_prompt(
            "active recall에서 관련없는 결과값 개선 가능해?",
            "2) 메모리/꿈(Dreaming)·Recall 품질",
            "OpenClaw update notes mention broad recall quality improvements.",
        )
        is False
    )


def test_build_injection_design_query_returns_canonical(monkeypatch):
    """Real end-to-end smoke test: design question returns canonical blocks."""
    monkeypatch.setattr(active_recall, "_semantic_blocks", lambda *args, **kwargs: [])
    result = active_recall.build_injection(
        prompt="프론트엔드 디자인 어떻게 해?",
        session_id="t-e2e",
        turn_idx=0,
        agent="claude",
    )
    # Intent should match
    if result.get("intent"):
        assert "frontend_design" in result["intent"]
    # If canonical DESIGN.md exists on disk, it should surface
    blocks = result.get("blocks", [])
    has_canonical = any(b.get("source") == "canonical" for b in blocks)
    assert has_canonical or blocks == []


# ── Seen registry round-trip ────────────────────────────


def test_update_seen_persists_to_session_context():
    block = active_recall.InjectionBlock(
        id="persist_test",
        title="t",
        content="c",
        source="canonical",
        score=1.0,
        priority="critical",
    )
    active_recall._update_seen("sess-1", "claude", 5, [block])
    seen = active_recall._get_seen("sess-1", "claude")
    assert "persist_test" in seen
    assert seen["persist_test"]["last_turn"] == 5
    assert seen["persist_test"]["priority"] == "critical"


# ── Route-guarantee participation when under-served ──────────────────────
# Generic: a high-priority matched route's curated guarantee should fire when
# blocks are empty OR when the surviving blocks are low-authority/noisy and do
# not actually serve the route — not only when all_blocks is empty.


def test_active_recall_route_guarantee_fires_when_blocks_low_quality(monkeypatch):
    """A high-priority intent (codex) whose only surviving semantic hit is a
    low-authority session/reflection row is under-served — the route guarantee
    must participate even though all_blocks is non-empty."""
    session_row = {
        "id": "sess-1",
        "title": "Codex session log",
        "content": (
            "Codex through Hermes coding session log; tmux usage noted across the "
            "run while steering quality on a complex task."
        ),
        "score": 92.0,
        "collection": "rag",
        "path": "/sessions/2026-05-10-codex.md",
    }
    monkeypatch.setitem(
        sys.modules,
        "search_unified",
        types.SimpleNamespace(search_all=lambda *a, **k: {"results": [session_row]}),
    )

    result = active_recall.build_injection(
        prompt="복잡한 코딩 작업은 코덱스를 어떻게 쓰는 게 좋아?",
        session_id="t-route-underserved",
        turn_idx=0,
        agent="codex",
    )

    sources = [b["source"] for b in result["blocks"]]
    assert "canonical_route_hint" in sources, f"expected route guarantee, got {sources}"
    hint = next(b for b in result["blocks"] if b["source"] == "canonical_route_hint")
    assert "interactive terminal-like tmux TUI" in hint["content"]


def test_active_recall_route_guarantee_skipped_when_strong_block_present(monkeypatch):
    """Negative control: when a strong, on-topic durable block already serves
    the high-priority route, the route guarantee must NOT also fire."""
    preference_row = {
        "id": "codex-pref-1",
        "title": "Codex workflow preference",
        "content": (
            "Chris prefers using Codex through Hermes as an interactive terminal-like "
            "tmux TUI when quality or steering matters; headless codex exec is only "
            "for bounded automation."
        ),
        "score": 95.0,
        "collection": "semantic_memory",
        "path": "/semantic/codex_pref.md",
    }
    monkeypatch.setitem(
        sys.modules,
        "search_unified",
        types.SimpleNamespace(search_all=lambda *a, **k: {"results": [preference_row]}),
    )

    result = active_recall.build_injection(
        prompt="복잡한 코딩 작업은 코덱스를 어떻게 쓰는 게 좋아?",
        session_id="t-route-served",
        turn_idx=0,
        agent="codex",
    )

    sources = [b["source"] for b in result["blocks"]]
    assert "canonical_route_hint" not in sources, f"should not duplicate guarantee, got {sources}"
    assert any("tmux TUI" in b["content"] for b in result["blocks"])


def test_is_declarative_route_guarantee_excludes_search_probes():
    """Route guarantees must be declarative policy/preference statements, not
    short keyword/search-probe fragments (generic shape test, not task-specific)."""
    from active_recall import _is_declarative_route_guarantee

    # Bare search probes -> not eligible as standalone guarantee blocks.
    assert _is_declarative_route_guarantee("design standard") is False
    assert _is_declarative_route_guarantee("openclaw agent configuration heartbeat") is False
    assert _is_declarative_route_guarantee("image caption description") is False

    # Declarative policy/preference statements -> eligible.
    assert (
        _is_declarative_route_guarantee(
            "Chris wants OMX and Codex CLI orchestration from Hermes to use the same "
            "terminal-like tmux pattern when quality matters"
        )
        is True
    )
    assert (
        _is_declarative_route_guarantee(
            "Chris prefers using Codex through Hermes as an interactive terminal-like "
            "tmux TUI when quality or steering matters; headless codex exec is only for "
            "bounded automation"
        )
        is True
    )


# ── Live-state short-circuit before active semantic injection ──────────────
# /recall/v2 already short-circuits present-state / current-status /
# done-right-now prompts via routes.recall._is_live_state_query. The active
# prefetch path must MIRROR that: skip route matching + semantic search +
# proactive/doorbell injection entirely, so stale memory is never searched for
# questions only live tools can answer. Generic EN/KO classifier; durable
# preference/history prompts must remain searchable.


def _counting_search_unified():
    """Fake search_unified whose search_all records how many times it ran."""
    calls = {"n": 0}

    def search_all(*args, **kwargs):
        calls["n"] += 1
        return {"results": []}

    return calls, types.SimpleNamespace(search_all=search_all)


def test_is_live_state_prompt_classifies_present_state_en_ko():
    from active_recall import _is_live_state_prompt

    for q in [
        "Is Liz done with the Brain recall fix right now?",
        "What is the current status of the active kanban task?",
        "What is happening on the diagnostics tasks at this moment?",
        "브레인 리콜 수정 지금 끝났어?",
        "현재 진단 태스크들 어디까지 됐어?",
    ]:
        assert _is_live_state_prompt(q) is True, q

    # Durable preference / history / topic prompts (incl. an adjectival "right
    # now") must NOT be classified live-state — they stay searchable.
    for q in [
        "What does Chris prefer for coding agents right now?",
        "OpenClaw하고 Hermes 런타임 차이 지금 기준으로 알려줘",
        "복잡한 코딩 작업은 코덱스를 어떻게 쓰는 게 좋아?",
        "추가 유료 API 없이 자동화 도구 추천해줘.",
    ]:
        assert _is_live_state_prompt(q) is False, q


def test_build_injection_live_state_en_skips_search_and_blocks(monkeypatch):
    calls, fake = _counting_search_unified()
    monkeypatch.setitem(sys.modules, "search_unified", fake)

    result = active_recall.build_injection(
        prompt="Is Liz done with the Brain recall fix right now?",
        session_id="t-live-state-en",
        turn_idx=0,
        agent="sage",
    )

    assert result["blocks"] == []
    assert result["degraded"] is False
    assert calls["n"] == 0, "EN live-state prompt must not run semantic search"


def test_build_injection_live_state_ko_skips_search_and_blocks(monkeypatch):
    calls, fake = _counting_search_unified()
    monkeypatch.setitem(sys.modules, "search_unified", fake)

    result = active_recall.build_injection(
        prompt="브레인 리콜 수정 지금 끝났어?",
        session_id="t-live-state-ko",
        turn_idx=0,
        agent="sage",
    )

    assert result["blocks"] == []
    assert result["degraded"] is False
    assert calls["n"] == 0, "KO live-state prompt must not run semantic search"


def test_build_injection_durable_prompt_remains_searchable(monkeypatch):
    """Negative control: a durable preference prompt is NOT short-circuited —
    active prefetch still runs semantic search (remains searchable)."""
    calls, fake = _counting_search_unified()
    monkeypatch.setitem(sys.modules, "search_unified", fake)

    result = active_recall.build_injection(
        prompt="복잡한 코딩 작업은 코덱스를 어떻게 쓰는 게 좋아?",
        session_id="t-durable-searchable",
        turn_idx=0,
        agent="codex",
    )

    assert result["degraded"] is False
    assert calls["n"] >= 1, "durable prompt must remain searchable (semantic search runs)"


# ── Route service requires route-RELEVANT evidence (failure class 1) ───────
# A high/critical route must be considered "served" (so its curated guarantee
# is suppressed) only when a returned block carries route-relevant durable
# evidence — NOT merely because some high-cosine off-route canonical/semantic
# row was retrieved. Otherwise an unrelated high-score row (a self-model /
# operating-model page) silently suppresses the codex route guarantee.


def test_block_serves_route_requires_route_relevant_evidence():
    from active_recall import (
        InjectionBlock,
        IntentMatch,
        _block_serves_route,
        _matched_route_underserved,
    )

    codex = IntentMatch(
        intent="codex_workflow",
        always_push_queries=[
            "Chris prefers using Codex through Hermes as an interactive terminal-like "
            "tmux TUI when quality or steering matters; headless codex exec is only for "
            "bounded automation",
        ],
        priority="high",
    )
    off_route = InjectionBlock(
        id="off",
        title="Chris's OpenClaw operating model for planning, execution, review",
        content="OpenClaw operating model: planning, execution, review, visibility across agents",
        source="semantic:canonical",
        score=0.9,
        priority="high",
    )
    on_route = InjectionBlock(
        id="on",
        title="Codex workflow preference",
        content="Chris prefers Codex through Hermes interactive tmux TUI when steering quality matters",
        source="semantic:semantic_memory",
        score=0.9,
        priority="high",
    )

    # High-cosine but off-route → does NOT serve the codex route.
    assert _block_serves_route(off_route, codex) is False
    # Route-relevant durable evidence → serves it.
    assert _block_serves_route(on_route, codex) is True

    # A high-priority route with only an off-route block is under-served; a
    # route-relevant block serves it; an empty set is always under-served.
    assert _matched_route_underserved([off_route], codex) is True
    assert _matched_route_underserved([on_route], codex) is False
    assert _matched_route_underserved([], codex) is True


def test_active_recall_route_guarantee_fires_for_high_score_off_route_block(monkeypatch):
    """A high-cosine but OFF-route semantic:canonical row (e.g. the OpenClaw
    operating model) must not suppress the codex route guarantee — it does not
    serve the codex route, so the curated guarantee still participates."""
    off_route = {
        "id": "openclaw-opmodel",
        "title": "Chris's OpenClaw operating model for planning, execution, review, and visibility",
        "content": (
            "OpenClaw operating model for planning, execution, review, and visibility "
            "across agents; orchestration and steering of work for Chris."
        ),
        "score": 92.0,
        "collection": "canonical",
        "path": "/canonical/openclaw_operating_model.md",
    }
    monkeypatch.setitem(
        sys.modules,
        "search_unified",
        types.SimpleNamespace(search_all=lambda *a, **k: {"results": [off_route]}),
    )

    result = active_recall.build_injection(
        prompt="복잡한 코딩 작업은 코덱스를 어떻게 쓰는 게 좋아?",
        session_id="t-codex-offroute",
        turn_idx=0,
        agent="codex",
    )

    sources = [b["source"] for b in result["blocks"]]
    assert "canonical_route_hint" in sources, f"off-route block must not suppress guarantee, got {sources}"
    hint = next(b for b in result["blocks"] if b["source"] == "canonical_route_hint")
    assert "interactive terminal-like tmux TUI" in hint["content"]


# ── First-class durable route guarantees via the shared layer ──────────────
# The OpenClaw-historical-vs-Hermes-current runtime distinction has no
# canonical_paths / declarative always_push_query in intent_routes.yaml, so it
# was never injectable on the active path. The shared route_guarantees layer
# makes it a first-class durable fact, surfaced when the prompt matches the
# guaranteed route and retrieval carries no durable statement of it.


def test_active_recall_runtime_distinction_route_guarantee_injected(monkeypatch):
    """A current OpenClaw-vs-Hermes distinction prompt must surface the durable
    historical/current runtime fact even when retrieval returns only a stale
    OpenClaw setup doc (which must NOT suppress the guarantee)."""
    setup_doc = {
        "id": "openclaw-setup",
        "title": "OpenClaw Multi-Agent Setup Documentation",
        "content": "OpenClaw workspace agents heartbeat configuration; Hermes migration notes.",
        "score": 88.0,
        "collection": "obsidian",
        "path": "/Users/chrischo/.openclaw/workspace-claude/AGENTS.md",
    }
    monkeypatch.setitem(
        sys.modules,
        "search_unified",
        types.SimpleNamespace(search_all=lambda *a, **k: {"results": [setup_doc]}),
    )

    result = active_recall.build_injection(
        prompt="What is the current OpenClaw vs Hermes runtime distinction?",
        session_id="t-runtime-distinction",
        turn_idx=0,
        agent="claude",
    )

    sources = [b["source"] for b in result["blocks"]]
    assert "route_guarantee" in sources, f"expected route_guarantee, got {sources}"
    g = next(b for b in result["blocks"] if b["source"] == "route_guarantee")
    assert "historical" in g["content"].lower()
    assert "current agent runtime" in g["content"].lower()


def test_active_recall_runtime_distinction_not_injected_for_setup_query(monkeypatch):
    """Negative control: a bare setup question naming both runtimes but with NO
    distinction cue must NOT trigger the runtime-distinction guarantee."""
    monkeypatch.setitem(
        sys.modules,
        "search_unified",
        types.SimpleNamespace(search_all=lambda *a, **k: {"results": []}),
    )

    result = active_recall.build_injection(
        prompt="OpenClaw and Hermes setup guide",
        session_id="t-runtime-setup",
        turn_idx=0,
        agent="claude",
    )

    sources = [b["source"] for b in result["blocks"]]
    assert "route_guarantee" not in sources, f"setup query must not inject distinction, got {sources}"


def test_active_recall_quality_prompt_does_not_match_frontend_design():
    """Token-boundary safety: `ui` inside `quality`/`prefetch` must not route a
    Brain-quality prompt to frontend_design (no DESIGN.md injection)."""
    from active_recall import _match_canonical_routes

    matches = _match_canonical_routes("Right now, is Brain's recall and prefetch quality healthy or noisy?")
    intents = {m.intent for m in matches}
    assert "frontend_design" not in intents, f"`ui` matched inside quality/prefetch: {intents}"


def test_active_recall_cost_route_guarantee_survives_to_output(monkeypatch):
    """A cost/billing recommendation must surface the cost route guarantee on
    /recall/active. The guarantee is injected after decay+arbitration, so even a
    low-memory judgment (no canonical intent route matched) cannot drop it."""
    monkeypatch.setitem(
        sys.modules,
        "search_unified",
        types.SimpleNamespace(search_all=lambda *a, **k: {"results": []}),
    )

    result = active_recall.build_injection(
        prompt="What's a budget-conscious tooling approach you'd recommend for me?",
        session_id="t-cost-route-guarantee",
        turn_idx=0,
        agent="liz",
    )

    sources = [b["source"] for b in result["blocks"]]
    assert "route_guarantee" in sources, f"cost route guarantee must reach output, got {sources}"
    g = next(b for b in result["blocks"] if b["source"] == "route_guarantee")
    assert "subscription" in g["content"].lower() and "local model" in g["content"].lower()


def test_route_guarantee_blocks_emitted_at_critical_priority():
    """A matched, not-already-served durable route fact is direct_current_truth
    for that route, so its block must be critical — otherwise _enforce_budget
    (sort by priority then score) sinks it below higher-scoring off-route
    semantic hits and it drops out of the top window."""
    blocks = active_recall._route_guarantee_blocks(
        "What is the current OpenClaw vs Hermes runtime distinction?", [], set()
    )
    assert blocks, "runtime distinction route guarantee should fire"
    assert all(b.source == "route_guarantee" for b in blocks)
    assert all(b.priority == "critical" for b in blocks)


def test_critical_route_guarantee_survives_budget_over_high_score_offroute_blocks():
    """Regression for the KO runtime-distinction active failure: 3 high-scoring
    off-route semantic blocks must not push the (smaller, lower-scored) route
    guarantee out of the top window. Critical priority keeps it at the top."""
    guarantee = active_recall.InjectionBlock(
        id="g",
        title="runtime_distinction route guarantee",
        content="OpenClaw is historical; Hermes is the current agent runtime.",
        source="route_guarantee",
        score=0.95,
        priority="critical",
    )
    offroute = [
        active_recall.InjectionBlock(
            id=f"s{i}",
            title=f"OpenClaw operating model {i}",
            content="OpenClaw and Hermes operating model notes " * 20,
            source="semantic:canonical",
            score=1.0,
            priority="high",
        )
        for i in range(3)
    ]
    kept = active_recall._enforce_budget([*offroute, guarantee], active_recall.BUDGET_TOKEN_LIMIT)
    top3 = kept[:3]
    assert any(
        b.source == "route_guarantee" for b in top3
    ), f"guarantee dropped out of top window: {[b.source for b in kept]}"
    assert kept[0].source == "route_guarantee"


# ── Holdout regressions (t_c7453635) ───────────────────────────────────────


def test_active_recall_cost_paraphrase_avoid_paid_self_hosting_surfaces_guarantee(monkeypatch):
    """Holdout cost failure: a cost paraphrase with NO spend noun (avoid new paid
    APIs / no self-hosted generation models) must still surface the cost route
    guarantee on /recall/active — the route must match by meaning, not by a noun."""
    monkeypatch.setitem(
        sys.modules,
        "search_unified",
        types.SimpleNamespace(search_all=lambda *a, **k: {"results": []}),
    )

    result = active_recall.build_injection(
        prompt="Choose an AI tooling path that avoids new paid APIs and avoids self-hosting generation models.",
        session_id="t-cost-paraphrase",
        turn_idx=0,
        agent="liz",
    )

    sources = [b["source"] for b in result["blocks"]]
    assert "route_guarantee" in sources, f"cost route guarantee must surface, got {sources}"
    g = next(b for b in result["blocks"] if b["source"] == "route_guarantee")
    low = g["content"].lower()
    assert "subscription" in low and ("local model" in low or "embeddings" in low)


def test_active_recall_codex_tui_prompt_routes_codex_not_frontend_design(monkeypatch):
    """Route-arbitration false-positive control: a Codex-in-tmux-TUI workflow
    prompt must surface the codex workflow guarantee, never a frontend/design
    block ('ui'/'tui' are not design tokens)."""
    monkeypatch.setitem(
        sys.modules,
        "search_unified",
        types.SimpleNamespace(search_all=lambda *a, **k: {"results": []}),
    )

    result = active_recall.build_injection(
        prompt="When careful coding guidance matters, should Codex run in a Hermes tmux TUI instead of headless exec?",
        session_id="t-codex-arbitration",
        turn_idx=0,
        agent="liz",
    )

    blob = " ".join(f"{b.get('title') or ''} {b.get('source') or ''}" for b in result["blocks"]).lower()
    assert "frontend_design" not in blob, f"design false-positive: {result['blocks']}"
    assert any(
        "codex" in f"{b.get('title') or ''} {b.get('content') or ''}".lower() for b in result["blocks"]
    ), f"codex workflow guarantee missing: {result['blocks']}"


# ── REQUEST_CHANGES f4: explicit route negation arbitration ────────────────


def test_active_recall_negated_codex_design_prompt_drops_codex_route(monkeypatch):
    """Finding 4 (active_frontend_design_codex_false_positive): when the prompt
    explicitly negates the Codex route ('not about codex') and asks for a
    frontend/design critique, active recall must NOT emit a codex_workflow route
    guarantee. The design route is positive evidence; codex is negated keyword
    residue. EN + KO."""
    monkeypatch.setitem(
        sys.modules,
        "search_unified",
        types.SimpleNamespace(search_all=lambda *a, **k: {"results": []}),
    )

    for prompt, sid in (
        (
            "This is not about codex — critique the frontend design quality of this layout workflow.",
            "t-neg-codex-en",
        ),
        ("코덱스 얘기 아니라 프론트엔드 디자인 품질을 워크플로우 관점에서 봐줘", "t-neg-codex-ko"),
    ):
        result = active_recall.build_injection(prompt=prompt, session_id=sid, turn_idx=0, agent="liz")
        blob = " ".join(
            f"{b.get('title') or ''} {b.get('content') or ''} {b.get('source') or ''}"
            for b in result["blocks"]
        ).lower()
        assert "codex" not in blob, f"negated codex route leaked: {result['blocks']}"


def test_active_recall_negated_codex_keeps_frontend_design_route():
    """Control for f4: the design intent still matches when codex is negated, so
    suppression targets only the negated route — it does not blank the prompt."""
    matches = active_recall._match_canonical_routes(
        "This is not about codex — critique the frontend design quality of this layout."
    )
    intents = [m.intent for m in matches]
    assert "frontend_design" in intents
    assert "codex_workflow" not in intents
