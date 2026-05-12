"""Unit tests for brain_loop._decide stage helpers.

Extracted on 2026-05-12 from the 75-line _decide function into four
focused helpers:
  - _resolve_autonomy_level
  - _eval_proposal_payload_from_downgrade
  - _apply_autonomy_downgrade
  - _agent_dispatch_disabled / _apply_agent_dispatch_disable

These tests pin the byte-equal contract so the next sense/decide/act
split stage can verify no behavior drift.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


@pytest.fixture
def _make_decision():
    """Factory for Decisions with a minimal Observation."""
    from brain_loop import Decision, DecisionKind, Observation

    def _factory(kind=DecisionKind.DISPATCH_AGENT, payload=None, requires="brain_loop.x"):
        obs = Observation(kind="stalled_goal", subject="g_1", evidence={"k": "v"})
        return Decision(
            observation=obs,
            kind=kind,
            action_payload=payload or {"agent": "ellie", "message": "hi"},
            reasoning="r",
            confidence=0.6,
            requires_autonomy=requires,
        )

    return _factory


# ── _resolve_autonomy_level ──────────────────────────────────────────


class _FakeAutonomyDecision:
    def __init__(self, level: str):
        self.level = level


def test_resolve_autonomy_level_returns_gate_level(monkeypatch, _make_decision):
    """Happy path: _authorize returns a decision with .level → that level
    is set on the Decision and returned."""
    import brain_loop

    captured: list = []
    monkeypatch.setattr(
        brain_loop, "_authorize", lambda key: captured.append(key) or _FakeAutonomyDecision("L2")
    )

    d = _make_decision(requires="brain_loop.dispatch_agent_checkin")
    level = brain_loop._resolve_autonomy_level(d)
    assert level == "L2"
    assert d.autonomy_level == "L2"
    assert captured == ["brain_loop.dispatch_agent_checkin"]


def test_resolve_autonomy_level_exception_falls_back_to_l0(monkeypatch, _make_decision):
    """A gate exception falls back to "L0" (most restrictive)."""
    import brain_loop

    def _boom(key):
        raise RuntimeError("autonomy db locked")

    monkeypatch.setattr(brain_loop, "_authorize", _boom)

    d = _make_decision()
    assert brain_loop._resolve_autonomy_level(d) == "L0"
    assert d.autonomy_level == "L0"


# ── _apply_autonomy_downgrade ────────────────────────────────────────


def test_apply_autonomy_downgrade_l0_forces_observe_only(_make_decision):
    """L0 → kind becomes OBSERVE_ONLY, reasoning tagged."""
    import brain_loop
    from brain_loop import DecisionKind

    d = _make_decision(DecisionKind.DISPATCH_AGENT)
    brain_loop._apply_autonomy_downgrade(d, "L0")
    assert d.kind == DecisionKind.OBSERVE_ONLY
    assert "[downgraded L0]" in d.reasoning


def test_apply_autonomy_downgrade_l1_rewrites_dispatch_to_propose(_make_decision):
    """L1 + DISPATCH_AGENT → flips to PROPOSE with eval_proposal payload."""
    import brain_loop
    from brain_loop import DecisionKind

    original = {"agent": "ellie", "message": "hi"}
    d = _make_decision(DecisionKind.DISPATCH_AGENT, payload=original)
    brain_loop._apply_autonomy_downgrade(d, "L1")

    assert d.kind == DecisionKind.PROPOSE
    assert "[downgraded L1]" in d.reasoning
    # Payload rewritten into eval_proposal shape:
    assert d.action_payload["evidence"]["observation_kind"] == "stalled_goal"
    assert d.action_payload["evidence"]["observation_subject"] == "g_1"
    assert d.action_payload["evidence"]["intended_payload"] == original
    # intended_kind captures d.kind.value at the time of payload build,
    # which is BEFORE d.kind flips to PROPOSE — preserves the original
    # action intent for the eval_proposal row.
    assert d.action_payload["evidence"]["intended_kind"] == DecisionKind.DISPATCH_AGENT.value
    # payload["reasoning"] is captured BEFORE the " [downgraded L1]" tag
    # gets appended to d.reasoning, so the captured value preserves the
    # pre-downgrade reasoning string.
    assert d.action_payload["reasoning"] == "r"
    assert d.action_payload["confidence"] == 0.6


def test_apply_autonomy_downgrade_l1_observe_only_unchanged(_make_decision):
    """L1 + OBSERVE_ONLY → no payload rewrite, no kind flip."""
    import brain_loop
    from brain_loop import DecisionKind

    d = _make_decision(DecisionKind.OBSERVE_ONLY, payload={"foo": "bar"})
    orig_kind = d.kind
    orig_payload = d.action_payload
    brain_loop._apply_autonomy_downgrade(d, "L1")
    assert d.kind == orig_kind
    assert d.action_payload is orig_payload
    assert "[downgraded L1]" not in d.reasoning


def test_apply_autonomy_downgrade_l1_propose_unchanged(_make_decision):
    """L1 + PROPOSE → no rewrite (already at PROPOSE)."""
    import brain_loop
    from brain_loop import DecisionKind

    d = _make_decision(DecisionKind.PROPOSE, payload={"a": 1})
    orig_payload = d.action_payload
    brain_loop._apply_autonomy_downgrade(d, "L1")
    assert d.kind == DecisionKind.PROPOSE
    assert d.action_payload is orig_payload


def test_apply_autonomy_downgrade_l2_or_above_is_noop(_make_decision):
    """L2+ → caller's original kind + payload stand."""
    import brain_loop
    from brain_loop import DecisionKind

    d = _make_decision(DecisionKind.PUSH_TO_CLAUDE, payload={"session_id": "s1"})
    brain_loop._apply_autonomy_downgrade(d, "L2")
    assert d.kind == DecisionKind.PUSH_TO_CLAUDE
    assert d.action_payload == {"session_id": "s1"}
    assert "[downgraded" not in d.reasoning


# ── _agent_dispatch_disabled ─────────────────────────────────────────


@pytest.mark.parametrize("val", ["1", "true", "True", "YES", " on "])
def test_agent_dispatch_disabled_on_truthy_values(monkeypatch, val):
    import brain_loop

    monkeypatch.setenv("BRAIN_LOOP_AGENT_DISPATCH_DISABLED", val)
    assert brain_loop._agent_dispatch_disabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
def test_agent_dispatch_disabled_on_falsy_values(monkeypatch, val):
    import brain_loop

    monkeypatch.setenv("BRAIN_LOOP_AGENT_DISPATCH_DISABLED", val)
    assert brain_loop._agent_dispatch_disabled() is False


def test_agent_dispatch_disabled_when_env_unset(monkeypatch):
    import brain_loop

    monkeypatch.delenv("BRAIN_LOOP_AGENT_DISPATCH_DISABLED", raising=False)
    assert brain_loop._agent_dispatch_disabled() is False


# ── _apply_agent_dispatch_disable ────────────────────────────────────


def test_apply_agent_dispatch_disable_on_dispatch_agent(monkeypatch, _make_decision):
    """env on + DISPATCH_AGENT → flips to PROPOSE with eval_proposal payload."""
    import brain_loop
    from brain_loop import DecisionKind

    monkeypatch.setenv("BRAIN_LOOP_AGENT_DISPATCH_DISABLED", "1")

    original = {"agent": "sage", "message": "investigate"}
    d = _make_decision(DecisionKind.DISPATCH_AGENT, payload=original)
    brain_loop._apply_agent_dispatch_disable(d)

    assert d.kind == DecisionKind.PROPOSE
    assert "[agent dispatch disabled]" in d.reasoning
    assert d.action_payload["evidence"]["intended_kind"] == DecisionKind.DISPATCH_AGENT.value
    assert d.action_payload["evidence"]["intended_payload"] == original


def test_apply_agent_dispatch_disable_env_off_is_noop(monkeypatch, _make_decision):
    import brain_loop
    from brain_loop import DecisionKind

    monkeypatch.setenv("BRAIN_LOOP_AGENT_DISPATCH_DISABLED", "off")

    original = {"agent": "sage", "message": "x"}
    d = _make_decision(DecisionKind.DISPATCH_AGENT, payload=original)
    brain_loop._apply_agent_dispatch_disable(d)

    assert d.kind == DecisionKind.DISPATCH_AGENT
    assert d.action_payload is original


def test_apply_agent_dispatch_disable_non_dispatch_kind_is_noop(monkeypatch, _make_decision):
    """Env on but kind != DISPATCH_AGENT → no-op."""
    import brain_loop
    from brain_loop import DecisionKind

    monkeypatch.setenv("BRAIN_LOOP_AGENT_DISPATCH_DISABLED", "1")

    d = _make_decision(DecisionKind.PUSH_TO_CLAUDE, payload={"x": 1})
    brain_loop._apply_agent_dispatch_disable(d)
    assert d.kind == DecisionKind.PUSH_TO_CLAUDE
    assert "[agent dispatch disabled]" not in d.reasoning


# ── _decide (smoke / wiring) ─────────────────────────────────────────


def test_decide_no_authorize_returns_empty(monkeypatch, _make_decision):
    """If _authorize is None (autonomy module unavailable), _decide
    returns [] — fail-closed for safety."""
    import brain_loop

    monkeypatch.setattr(brain_loop, "_authorize", None)
    assert brain_loop._decide([_make_decision()]) == []


def test_decide_pipeline_applies_downgrade_then_rate_limits(monkeypatch, _make_decision):
    """End-to-end smoke: gate returns L1 → DISPATCH_AGENT downgraded to
    PROPOSE → rate-limit gate fires → decision is approved."""
    import brain_loop
    from brain_loop import DecisionKind

    monkeypatch.setattr(brain_loop, "_authorize", lambda key: _FakeAutonomyDecision("L1"))
    monkeypatch.setattr(brain_loop, "_rate_limit_check", lambda key: True)

    d = _make_decision(DecisionKind.DISPATCH_AGENT)
    out = brain_loop._decide([d])
    assert len(out) == 1
    assert out[0].kind == DecisionKind.PROPOSE
    assert out[0].autonomy_level == "L1"


def test_decide_rate_limited_decisions_dropped(monkeypatch, _make_decision):
    """When _rate_limit_check returns False, the decision is dropped and
    _mark_seen_with_short_cooldown fires for the observation."""
    import brain_loop

    monkeypatch.setattr(brain_loop, "_authorize", lambda key: _FakeAutonomyDecision("L2"))
    monkeypatch.setattr(brain_loop, "_rate_limit_check", lambda key: False)

    seen: list = []
    monkeypatch.setattr(
        brain_loop,
        "_mark_seen_with_short_cooldown",
        lambda kind, subj: seen.append((kind, subj)),
    )

    d = _make_decision()
    assert brain_loop._decide([d]) == []
    assert seen == [("stalled_goal", "g_1")]
