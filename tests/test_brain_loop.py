"""tests/test_brain_loop.py — unit tests for the executive cortex.

Verifies sensor behavior, decision mapping, dedup/cooldown, and the decide
gate downgrade semantics. Uses in-memory sqlite fixtures so tests don't
touch the real brain.db / autonomy.db.

Run:
  .venv/bin/python -m pytest tests/test_brain_loop.py -q
"""

from __future__ import annotations

import json
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


def test_backlog_drain_stuck_false_when_failures_reduce_queue(tmp_path, monkeypatch):
    """A drain that is failing/abandoning old rows is degraded but not wedged."""
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    monkeypatch.setattr(brain_loop, "BRAIN_LOGS_DIR", tmp_path)
    (jobs / "llm_backlog_drain.log").write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {"drained": 0, "failed": 0, "abandoned": 0, "pending_after": 118},
                {"drained": 0, "failed": 5, "abandoned": 0, "pending_after": 113},
                {"drained": 0, "failed": 0, "abandoned": 1, "pending_after": 112},
            ]
        )
        + "\n"
    )

    assert brain_loop._is_backlog_drain_stuck() is False


def test_backlog_drain_stuck_true_after_three_zero_progress_cycles(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    monkeypatch.setattr(brain_loop, "BRAIN_LOGS_DIR", tmp_path)
    (jobs / "llm_backlog_drain.log").write_text(
        "\n".join(
            json.dumps({"drained": 0, "failed": 0, "abandoned": 0, "pending_after": n})
            for n in [118, 118, 119]
        )
        + "\n"
    )

    assert brain_loop._is_backlog_drain_stuck() is True


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


def test_reflect_llm_usage_spike_no_session_observes_below_governor_threshold():
    """LLM spike + no active claude should not page Chris when self-governor
    threshold is not met."""
    obs = brain_loop.Observation(
        kind="llm_usage_spike",
        subject="last_hour_95",
        evidence={"hourly_rate": 95, "baseline_per_hour": 20, "ratio": 4.75, "daily_total": 480},
    )
    decisions = brain_loop._reflect([obs])
    assert [d.kind for d in decisions] == [brain_loop.DecisionKind.OBSERVE_ONLY]


def test_reflect_proactive_urgent_without_session_dispatches_sage():
    obs = brain_loop.Observation(
        kind="proactive_urgent",
        subject="insight-1",
        evidence={"summary": "Potential correction loop", "detail": "Agents can resolve this."},
    )
    decisions = brain_loop._reflect([obs])
    assert len(decisions) == 1
    assert decisions[0].kind == brain_loop.DecisionKind.DISPATCH_AGENT
    assert decisions[0].action_payload["agent"] == "sage"


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


def test_brain_loop_alert_self_handles_with_subscription_agent(monkeypatch):
    agent_calls = []
    telegram_calls = []

    monkeypatch.setattr(
        brain_loop, "_dispatch_agent", lambda agent, message: agent_calls.append((agent, message)) or True
    )

    class _Telegram:
        @staticmethod
        def send_chris_telegram(*args, **kwargs):
            telegram_calls.append((args, kwargs))
            return True

    monkeypatch.setitem(sys.modules, "telegram_alert", _Telegram)

    assert brain_loop._telegram_alert("Breaker OPEN: qdrant transient timeout") is True
    assert len(agent_calls) == 1
    assert agent_calls[0][0] == "sage"
    assert telegram_calls == []


def test_brain_loop_alert_notifies_for_human_only_blocker(monkeypatch):
    agent_calls = []
    telegram_calls = []

    monkeypatch.setattr(
        brain_loop, "_dispatch_agent", lambda agent, message: agent_calls.append((agent, message)) or True
    )

    class _Telegram:
        @staticmethod
        def send_chris_telegram(*args, **kwargs):
            telegram_calls.append((args, kwargs))
            return True

    monkeypatch.setitem(sys.modules, "telegram_alert", _Telegram)

    assert brain_loop._telegram_alert("Need Chris 2FA code for account login") is True
    assert agent_calls == []
    assert len(telegram_calls) == 1


def test_tick_reentrancy_guard_skips_overlap():
    """If tick_lock can't be acquired, tick() returns 'overlap_skipped'."""
    loop = brain_loop.BrainLoop()
    loop._tick_lock.acquire()
    try:
        result = loop.tick()
        assert result["status"] == "overlap_skipped"
    finally:
        loop._tick_lock.release()


# ── Phase 1: incremental canonical index sensor ─────────────────


def test_sense_canonical_changed_disabled_returns_empty(monkeypatch):
    monkeypatch.setattr(brain_loop, "_INCREMENTAL_INDEX_BUS_ENABLED", False)
    assert brain_loop._sense_canonical_changed() == []


def test_sense_canonical_changed_rate_limited(monkeypatch):
    """Within MIN_INTERVAL_S of the last run, the sensor must NOT fire even
    when files newer than last_ts exist. Rate limiting protects qdrant from
    re-embed storms on rapid-fire edits.
    """
    import time as _t

    monkeypatch.setattr(brain_loop, "_INCREMENTAL_INDEX_BUS_ENABLED", True)
    monkeypatch.setattr(brain_loop, "_get_incremental_last_ts", lambda: _t.time() - 10)
    monkeypatch.setattr(brain_loop, "_max_canonical_mtime", lambda: (_t.time(), 5))
    obs = brain_loop._sense_canonical_changed()
    assert obs == []


def test_sense_canonical_changed_no_new_files_returns_empty(monkeypatch):
    """If max_mtime <= last_ts, nothing is newer — don't emit."""
    monkeypatch.setattr(brain_loop, "_INCREMENTAL_INDEX_BUS_ENABLED", True)
    monkeypatch.setattr(brain_loop, "_get_incremental_last_ts", lambda: 1000.0)
    monkeypatch.setattr(brain_loop, "_max_canonical_mtime", lambda: (900.0, 7))
    monkeypatch.setattr(brain_loop, "_INCREMENTAL_INDEX_MIN_INTERVAL_S", 0.0)
    obs = brain_loop._sense_canonical_changed()
    assert obs == []


def test_sense_canonical_changed_emits_when_files_newer(monkeypatch):
    """Newer files past the rate-limit window emit the canonical_changed obs
    that maps to the incremental_canonical_index decision in reflect.
    """
    import time as _t

    monkeypatch.setattr(brain_loop, "_INCREMENTAL_INDEX_BUS_ENABLED", True)
    monkeypatch.setattr(brain_loop, "_INCREMENTAL_INDEX_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(brain_loop, "_get_incremental_last_ts", lambda: 1000.0)
    monkeypatch.setattr(brain_loop, "_max_canonical_mtime", lambda: (_t.time(), 12))
    obs = brain_loop._sense_canonical_changed()
    assert len(obs) == 1
    assert obs[0].kind == "canonical_changed"
    assert obs[0].evidence["scanned"] == 12


def test_reflect_canonical_changed_produces_self_modify():
    """Observation kind=canonical_changed → SELF_MODIFY decision with
    modification=incremental_canonical_index.
    """
    obs = brain_loop.Observation(
        kind="canonical_changed",
        subject="canonical_or_distilled",
        evidence={"max_mtime": 1.0, "last_ts": 0.0, "scanned": 3},
        salience=0.4,
    )
    decisions = brain_loop._reflect([obs])
    assert any(
        d.kind == brain_loop.DecisionKind.SELF_MODIFY
        and d.action_payload.get("modification") == "incremental_canonical_index"
        for d in decisions
    )


def test_apply_self_modification_incremental_calls_indexer(monkeypatch):
    """The apply branch must call indexer.add_documents twice (canonical+distilled)
    with force_incremental=True and persist last_ts on success.
    """
    captured: list[tuple] = []

    fake_indexer = type(sys)("indexer")
    fake_indexer.add_documents = lambda name, docs, skip_stale_cleanup=False, force_incremental=False: (
        captured.append((name, len(docs), force_incremental)) or len(docs)
    )
    fake_indexer.collect_canonical = lambda: [
        {"content": "x", "type": "canonical-note"},
        {"content": "y", "type": "distilled-note"},
    ]
    fake_indexer.ensure_collection = lambda _name: None
    monkeypatch.setitem(sys.modules, "indexer", fake_indexer)

    persisted: list[float] = []
    monkeypatch.setattr(brain_loop, "_set_incremental_last_ts", lambda ts: persisted.append(ts))

    ok = brain_loop._apply_self_modification({"modification": "incremental_canonical_index"})
    assert ok is True

    names_called = [c[0] for c in captured]
    assert "canonical" in names_called and "distilled" in names_called
    assert all(c[2] is True for c in captured)
    assert len(persisted) == 1
