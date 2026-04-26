"""tests/test_brain_loop.py — unit tests for the executive cortex.

Verifies sensor behavior, decision mapping, dedup/cooldown, and the decide
gate downgrade semantics. Uses in-memory sqlite fixtures so tests don't
touch the real brain.db / autonomy.db.

Run:
  .venv/bin/python -m pytest tests/test_brain_loop.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "brain_core"))

import brain_loop

# ── Fixtures ──────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_seen_table(tmp_path, monkeypatch):
    """Redirect the brain_loop_seen SQL writes to a temp sqlite so the
    persistent cooldown table doesn't leak between tests."""
    fake_db = tmp_path / "autonomy.db"
    monkeypatch.setattr(brain_loop, "AUTONOMY_DB", fake_db)
    # Reset the one-shot schema flag so _ensure_seen_schema creates table
    # in the new fake_db.
    brain_loop._seen_schema_ready = False
    yield


@pytest.fixture
def sample_obs():
    return brain_loop.Observation(
        kind="test_kind",
        subject="test_subject",
        evidence={"k": "v"},
        salience=0.5,
        ts=brain_loop._now_iso(),
    )


# ── Seen / cooldown tests ────────────────────────────────


def test_mark_seen_persists_across_calls(sample_obs):
    """First mark_seen → row present; seen_recently returns True."""
    assert brain_loop._seen_recently(sample_obs.kind, sample_obs.subject) is False
    brain_loop._mark_seen(sample_obs.kind, sample_obs.subject)
    assert brain_loop._seen_recently(sample_obs.kind, sample_obs.subject) is True


def test_filter_seen_drops_duplicates(sample_obs):
    """_filter_seen should pass the first observation but drop the second
    within cooldown."""
    survivors_1 = brain_loop._filter_seen([sample_obs])
    assert len(survivors_1) == 1
    survivors_2 = brain_loop._filter_seen([sample_obs])
    assert len(survivors_2) == 0


def test_seen_cooldown_distinct_per_subject():
    """Different subjects under same kind should not shadow each other."""
    a = brain_loop.Observation(kind="breaker_open", subject="chroma", salience=1.0, ts="")
    b = brain_loop.Observation(kind="breaker_open", subject="neo4j", salience=1.0, ts="")
    kept = brain_loop._filter_seen([a, b])
    assert len(kept) == 2
    # Second pass → both drop
    kept2 = brain_loop._filter_seen([a, b])
    assert len(kept2) == 0


def test_seen_cooldown_default_used_for_unknown_kind():
    """Unknown kinds should fall through to DEFAULT_COOLDOWN_S without crash."""
    obs = brain_loop.Observation(kind="brand_new_kind", subject="x", salience=0.5, ts="")
    kept = brain_loop._filter_seen([obs])
    assert kept == [obs]  # first pass
    kept = brain_loop._filter_seen([obs])
    assert kept == []  # second pass, default cooldown blocks


# ── Reflect tests ────────────────────────────────────────


def test_reflect_stalled_goal_chris_owned_pushes_to_claude():
    """Chris-owned stalled goal + active claude session → PUSH_TO_CLAUDE."""
    obs_goal = brain_loop.Observation(
        kind="stalled_goal",
        subject="goal-123",
        evidence={"title": "refactor brain", "age_hours": 5, "owner": "chris"},
    )
    obs_claude = brain_loop.Observation(
        kind="claude_active",
        subject="session-xyz",
        evidence={},
    )
    decisions = brain_loop._reflect([obs_goal, obs_claude])
    push_decisions = [d for d in decisions if d.kind == brain_loop.DecisionKind.PUSH_TO_CLAUDE]
    assert len(push_decisions) == 1
    assert push_decisions[0].action_payload["session_id"] == "session-xyz"


def test_reflect_stalled_goal_agent_owned_dispatches_agent():
    """Agent-owned stalled goal → DISPATCH_AGENT."""
    obs = brain_loop.Observation(
        kind="stalled_goal",
        subject="goal-456",
        evidence={"title": "publish blog", "age_hours": 3, "owner": "market"},
    )
    decisions = brain_loop._reflect([obs])
    dispatch_decisions = [d for d in decisions if d.kind == brain_loop.DecisionKind.DISPATCH_AGENT]
    assert len(dispatch_decisions) == 1
    assert dispatch_decisions[0].action_payload["agent"] == "market"


def test_reflect_breaker_open_always_telegrams():
    """Breaker open always produces a TELEGRAM_ALERT regardless of claude state."""
    obs = brain_loop.Observation(
        kind="breaker_open",
        subject="ollama",
        evidence={"failures": 5, "reason": "connection refused"},
        salience=1.0,
    )
    decisions = brain_loop._reflect([obs])
    assert any(d.kind == brain_loop.DecisionKind.TELEGRAM_ALERT for d in decisions)


def test_reflect_accuracy_drop_proposes_self_modify():
    """accuracy_drop → SELF_MODIFY proposal (autonomy gate will downgrade to
    PROPOSE if needed at decide time)."""
    obs = brain_loop.Observation(
        kind="accuracy_drop",
        subject="coding",
        evidence={"accuracy": 0.45, "total": 20, "overrides": 8},
    )
    decisions = brain_loop._reflect([obs])
    sm = [d for d in decisions if d.kind == brain_loop.DecisionKind.SELF_MODIFY]
    assert len(sm) == 1
    assert sm[0].action_payload["modification"] == "autonomy_demote"


def test_reflect_contradiction_dispatches_sage():
    obs = brain_loop.Observation(
        kind="contradiction",
        subject="abc",
        evidence={"summary": "React vs Vue conflict"},
    )
    decisions = brain_loop._reflect([obs])
    agent_dispatches = [
        d
        for d in decisions
        if d.kind == brain_loop.DecisionKind.DISPATCH_AGENT and d.action_payload.get("agent") == "sage"
    ]
    assert len(agent_dispatches) == 1


def test_reflect_llm_usage_spike_no_session_falls_to_telegram():
    """LLM spike + no active claude → Telegram alert path."""
    obs = brain_loop.Observation(
        kind="llm_usage_spike",
        subject="last_hour_95",
        evidence={"hourly_rate": 95, "baseline_per_hour": 20, "ratio": 4.75, "daily_total": 480},
    )
    decisions = brain_loop._reflect([obs])
    tele = [d for d in decisions if d.kind == brain_loop.DecisionKind.TELEGRAM_ALERT]
    assert len(tele) == 1
    assert "95" in tele[0].action_payload["body"]


def test_reflect_empty_observations_returns_empty():
    assert brain_loop._reflect([]) == []


# ── Decide gate tests ────────────────────────────────────


def test_decide_downgrades_l0_to_observe():
    """When autonomy.authorize returns L0, the decision downgrades to OBSERVE_ONLY."""
    d = brain_loop.Decision(
        observation=brain_loop.Observation(kind="contradiction", subject="abc"),
        kind=brain_loop.DecisionKind.DISPATCH_AGENT,
        action_payload={"agent": "sage"},
        requires_autonomy="brain_loop.dispatch_agent_investigation",
    )

    class _FakeDecision:
        level = "L0"
        allowed = False

    with patch.object(brain_loop, "_authorize", return_value=_FakeDecision()):
        approved = brain_loop._decide([d])
    assert len(approved) == 1
    assert approved[0].kind == brain_loop.DecisionKind.OBSERVE_ONLY


def test_decide_downgrades_l1_to_propose():
    """L1 downgrades DISPATCH/PUSH/TELEGRAM/SELF_MODIFY → PROPOSE."""
    d = brain_loop.Decision(
        observation=brain_loop.Observation(kind="stalled_goal", subject="goal-9"),
        kind=brain_loop.DecisionKind.DISPATCH_AGENT,
        action_payload={"agent": "liz"},
        requires_autonomy="brain_loop.dispatch_agent_checkin",
    )

    class _FakeDecision:
        level = "L1"
        allowed = True

    with patch.object(brain_loop, "_authorize", return_value=_FakeDecision()):
        approved = brain_loop._decide([d])
    assert len(approved) == 1
    assert approved[0].kind == brain_loop.DecisionKind.PROPOSE


def test_decide_l2_preserves_kind():
    """L2 passes the decision through without downgrade."""
    d = brain_loop.Decision(
        observation=brain_loop.Observation(kind="breaker_open", subject="ollama"),
        kind=brain_loop.DecisionKind.TELEGRAM_ALERT,
        action_payload={"body": "test"},
        requires_autonomy="brain_loop.telegram_urgent",
    )

    class _FakeDecision:
        level = "L2"
        allowed = True

    with patch.object(brain_loop, "_authorize", return_value=_FakeDecision()):
        approved = brain_loop._decide([d])
    assert len(approved) == 1
    assert approved[0].kind == brain_loop.DecisionKind.TELEGRAM_ALERT


def test_decide_rate_limits_repeated_pair():
    """Per (kind, subject) rate limit caps at 3/hour."""

    class _FakeDecision:
        level = "L3"
        allowed = True

    # Reset in-memory rate limit dict
    brain_loop._rate_limits.clear()

    obs = brain_loop.Observation(kind="observe", subject="repeat")
    decision = brain_loop.Decision(
        observation=obs,
        kind=brain_loop.DecisionKind.OBSERVE_ONLY,
        requires_autonomy="brain_loop.observe",
    )
    with patch.object(brain_loop, "_authorize", return_value=_FakeDecision()):
        # Three consecutive calls allowed
        for _ in range(3):
            approved = brain_loop._decide([decision])
            assert len(approved) == 1
        # Fourth within window dropped
        approved = brain_loop._decide([decision])
        assert len(approved) == 0


# ── Tick end-to-end (mocked sensors) ─────────────────────


def test_tick_runs_all_phases_without_crash(monkeypatch):
    """tick() should run PERCEIVE → REFLECT → DECIDE → ACT → JOURNAL even when
    all sensors return empty. Validates the structural invariants."""
    empty_sensors = [(name, lambda: []) for name, _ in brain_loop.SENSORS]
    monkeypatch.setattr(brain_loop, "SENSORS", empty_sensors)
    monkeypatch.setattr(brain_loop, "_journal", lambda *a, **kw: None)

    loop = brain_loop.BrainLoop()
    result = loop.tick()
    assert result["status"] == "ok"
    assert result["observations"] == 0
    assert result["decisions"] == 0
    assert result["acted"] == 0


def test_tick_env_killswitch_returns_disabled(monkeypatch):
    """BRAIN_AUTOPILOT_DISABLED=1 → tick() returns status='disabled_env' without running."""
    monkeypatch.setenv("BRAIN_AUTOPILOT_DISABLED", "1")
    loop = brain_loop.BrainLoop()
    result = loop.tick()
    assert result["status"] == "disabled_env"


def test_act_records_decision_ledger(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        brain_loop,
        "_build_belief_state",
        lambda limit=5: {"version": 1, "summary": {"beliefs": 1}, "goals": [], "uncertainties": []},
    )
    monkeypatch.setattr(
        brain_loop,
        "_record_decision_ledger",
        lambda **kwargs: recorded.append(kwargs) or "decision-test",
    )
    monkeypatch.setattr(brain_loop, "_insert_action_audit", lambda **kwargs: 42)

    decision = brain_loop.Decision(
        observation=brain_loop.Observation(
            kind="contradiction",
            subject="c1",
            evidence={"domain": "brain"},
        ),
        kind=brain_loop.DecisionKind.OBSERVE_ONLY,
        reasoning="Abstain until reviewed.",
        confidence=0.6,
        autonomy_level="L1",
    )

    results = brain_loop._act([decision])

    assert results[0]["result"]["status"] == "observed"
    assert recorded[0]["domain"] == "brain"
    assert recorded[0]["selected_option"] == "observe"
    assert recorded[0]["autonomy_level"] == "L1"
    assert recorded[0]["perceived_state"]["belief_state"]["summary"]["beliefs"] == 1
    assert recorded[0]["action_audit_id"] == 42


def test_tick_reentrancy_guard_skips_overlap():
    """If tick_lock can't be acquired, tick() returns 'overlap_skipped'."""
    loop = brain_loop.BrainLoop()
    loop._tick_lock.acquire()
    try:
        result = loop.tick()
        assert result["status"] == "overlap_skipped"
    finally:
        loop._tick_lock.release()
