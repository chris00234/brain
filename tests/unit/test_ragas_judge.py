from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core"))

import ragas_judge


def test_ragas_judge_uses_cli_first_dispatch_without_openclaw_forcing(monkeypatch):
    calls = []

    def fake_dispatch(agent, message, **kwargs):
        calls.append({"agent": agent, "message": message, **kwargs})
        return SimpleNamespace(ok=True, text='{"score": 1.0, "reason": "supported"}')

    monkeypatch.setitem(sys.modules, "cli_llm", SimpleNamespace(dispatch=fake_dispatch))

    out = ragas_judge._dispatch_judge("judge this", timeout=7)

    assert out == '{"score": 1.0, "reason": "supported"}'
    assert calls
    call = calls[0]
    assert call["agent"] == "sage"
    assert call["timeout"] == 7
    assert call["openclaw_agent"] == "sage"
    assert call["backlog_kind"] == "synthesis"
    assert call["backlog_payload"]["source"] == "ragas_judge"
    assert "backend" not in call
    assert "max_backends" not in call
    assert "openclaw_session_id" not in call


def test_ragas_stats_advertises_codex_primary():
    stats = ragas_judge.stats()

    assert "codex/gpt-5.5 primary" in stats["judge_model"]


def test_answer_relevance_prompt_includes_expected_rubric(monkeypatch):
    prompts = []

    def fake_dispatch(prompt, timeout=30):
        prompts.append(prompt)
        return '{"score": 1.0, "reason": "rubric satisfied"}'

    monkeypatch.setattr(ragas_judge, "_dispatch_judge", fake_dispatch)

    score = ragas_judge.score_one(
        "What is the policy?",
        "Use CLI-first gpt-5.5 and send task evaluation action summaries.",
        ["CLI-first gpt-5.5 is the policy."],
        expected="Must mention CLI-first gpt-5.5 and action summaries.",
        metrics=["answer_relevance"],
    )

    assert score.answer_relevance == 1.0
    assert "Expected answer/rubric: Must mention CLI-first gpt-5.5" in prompts[0]
