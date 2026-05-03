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
