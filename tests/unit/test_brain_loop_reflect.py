"""Unit tests for brain_loop._reflect handler dispatch.

Extracted on 2026-05-12 from the 400-line _reflect if-elif chain into 14
per-kind handlers + a _REFLECT_HANDLERS dispatch table. These tests pin
the byte-equal contract of each handler (kind, action_payload shape,
reasoning text fragment, confidence, requires_autonomy) so the next stage
of the brain_loop sense/decide/act split can verify no behavior drift.
"""

from __future__ import annotations

import sys
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


def _obs(kind: str, subject: str = "subj", evidence: dict | None = None):
    from brain_loop import Observation

    return Observation(kind=kind, subject=subject, evidence=evidence or {})


# ── _reflect_stalled_goal ─────────────────────────────────────────────


def test_stalled_goal_chris_owned_with_session_pushes_doorbell():
    from brain_loop import DecisionKind, _reflect_stalled_goal

    o = _obs("stalled_goal", "g_1", {"owner": "chris", "title": "ship X", "age_hours": 4.2})
    decisions = _reflect_stalled_goal(o, claude_session="sess_active")
    assert len(decisions) == 1
    d = decisions[0]
    assert d.kind == DecisionKind.PUSH_TO_CLAUDE
    assert d.action_payload["session_id"] == "sess_active"
    assert d.action_payload["title"] == "Stalled goal: ship X"
    assert "4.2h" in d.action_payload["content"]
    assert d.action_payload["priority"] == "high"
    assert d.confidence == 0.8
    assert d.requires_autonomy == "brain_loop.push_to_claude"


def test_stalled_goal_agent_owned_dispatches_agent():
    from brain_loop import DecisionKind, _reflect_stalled_goal

    o = _obs("stalled_goal", "g_1", {"owner": "ellie", "title": "fix infra", "age_hours": 8})
    decisions = _reflect_stalled_goal(o, claude_session=None)
    assert decisions[0].kind == DecisionKind.DISPATCH_AGENT
    assert decisions[0].action_payload["agent"] == "ellie"
    assert "8.0h" in decisions[0].action_payload["message"]
    assert decisions[0].confidence == 0.7


def test_stalled_goal_chris_owned_no_session_observe_only():
    from brain_loop import DecisionKind, _reflect_stalled_goal

    o = _obs("stalled_goal", "g_1", {"owner": "chris", "title": "x", "age_hours": 4})
    decisions = _reflect_stalled_goal(o, claude_session=None)
    assert decisions[0].kind == DecisionKind.OBSERVE_ONLY
    assert decisions[0].confidence == 0.5


# ── _reflect_recall_miss ──────────────────────────────────────────────


def test_recall_miss_proposes_eval_candidate():
    from brain_loop import DecisionKind, _reflect_recall_miss

    o = _obs("recall_miss", "sess_42", {"query": "x"})
    decisions = _reflect_recall_miss(o, None)
    assert len(decisions) == 1
    d = decisions[0]
    assert d.kind == DecisionKind.PROPOSE
    assert d.action_payload["category"] == "intent_route_candidate"
    assert d.action_payload["session_id"] == "sess_42"
    assert d.action_payload["evidence"] == {"query": "x"}
    assert d.confidence == 0.9


# ── _reflect_breaker_open ─────────────────────────────────────────────


def test_breaker_open_alerts_urgent_telegram():
    from brain_loop import DecisionKind, _reflect_breaker_open

    o = _obs("breaker_open", "qdrant.search", {"failures": 12, "reason": "connection refused"})
    decisions = _reflect_breaker_open(o, None)
    d = decisions[0]
    assert d.kind == DecisionKind.TELEGRAM_ALERT
    assert d.action_payload["severity"] == "urgent"
    assert "qdrant.search" in d.action_payload["body"]
    assert "12" in d.action_payload["body"]
    assert d.confidence == 1.0


def test_breaker_open_truncates_reason_at_200():
    from brain_loop import _reflect_breaker_open

    long_reason = "x" * 1000
    o = _obs("breaker_open", "subj", {"reason": long_reason})
    d = _reflect_breaker_open(o, None)[0]
    # body contains "reason: <reason[:200]>"
    reason_segment = d.action_payload["body"].split("reason: ", 1)[1]
    assert len(reason_segment) == 200


# ── _reflect_accuracy_drop ────────────────────────────────────────────


def test_accuracy_drop_self_modifies_autonomy_demote():
    from brain_loop import DecisionKind, _reflect_accuracy_drop

    o = _obs("accuracy_drop", "intent_route", {"accuracy": 0.42, "total": 50})
    d = _reflect_accuracy_drop(o, None)[0]
    assert d.kind == DecisionKind.SELF_MODIFY
    assert d.action_payload["modification"] == "autonomy_demote"
    assert d.action_payload["domain"] == "intent_route"
    assert d.action_payload["to_level"] == "L1"
    assert "42%" in d.action_payload["reason"]


# ── _reflect_contradiction ────────────────────────────────────────────


def test_contradiction_dispatches_to_sage():
    from brain_loop import DecisionKind, _reflect_contradiction

    o = _obs("contradiction", "c1", {"summary": "atom A says X, atom B says Y"})
    d = _reflect_contradiction(o, None)[0]
    assert d.kind == DecisionKind.DISPATCH_AGENT
    assert d.action_payload["agent"] == "sage"
    assert "atom A says X" in d.action_payload["message"]


def test_contradiction_truncates_summary_at_200():
    from brain_loop import _reflect_contradiction

    long_summary = "x" * 1000
    o = _obs("contradiction", "c1", {"summary": long_summary})
    d = _reflect_contradiction(o, None)[0]
    msg = d.action_payload["message"]
    # message uses {summary[:200]}\n... → truncated portion is exactly 200 chars
    assert "x" * 200 in msg
    assert "x" * 201 not in msg


# ── _reflect_llm_usage_spike ──────────────────────────────────────────


def test_llm_usage_spike_governor_engaged_when_severe_and_no_session(monkeypatch):
    """ratio>=5 AND no session AND governor enabled → governor SELF_MODIFY
    fires. No PUSH_TO_CLAUDE in this branch."""
    from brain_loop import DecisionKind, _reflect_llm_usage_spike

    monkeypatch.setenv("BRAIN_LLM_COST_GOVERNOR", "on")
    o = _obs("llm_usage_spike", "global", {"hourly_rate": 500, "baseline_per_hour": 100, "ratio": 5})
    decisions = _reflect_llm_usage_spike(o, claude_session=None)
    # 1 governor SELF_MODIFY + 1 OBSERVE_ONLY (severe path, no session)
    kinds = [d.kind for d in decisions]
    assert DecisionKind.SELF_MODIFY in kinds
    assert DecisionKind.OBSERVE_ONLY in kinds
    gov = next(d for d in decisions if d.kind == DecisionKind.SELF_MODIFY)
    assert gov.action_payload["modification"] == "engage_llm_cost_governor"
    assert gov.action_payload["ttl_s"] == 1800


def test_llm_usage_spike_governor_disabled_when_env_off(monkeypatch):
    from brain_loop import DecisionKind, _reflect_llm_usage_spike

    monkeypatch.setenv("BRAIN_LLM_COST_GOVERNOR", "off")
    o = _obs("llm_usage_spike", "global", {"ratio": 10})
    decisions = _reflect_llm_usage_spike(o, claude_session=None)
    # No SELF_MODIFY now — only the OBSERVE_ONLY ratio>=5 branch
    assert all(d.kind != DecisionKind.SELF_MODIFY for d in decisions)


def test_llm_usage_spike_active_session_pushes_doorbell(monkeypatch):
    """Active session always wins over OBSERVE_ONLY. Governor may also
    fire when ratio is severe."""
    from brain_loop import DecisionKind, _reflect_llm_usage_spike

    monkeypatch.setenv("BRAIN_LLM_COST_GOVERNOR", "on")
    o = _obs("llm_usage_spike", "global", {"hourly_rate": 200, "baseline_per_hour": 100, "ratio": 2})
    decisions = _reflect_llm_usage_spike(o, claude_session="sess_x")
    assert any(d.kind == DecisionKind.PUSH_TO_CLAUDE for d in decisions)
    assert all(d.kind != DecisionKind.OBSERVE_ONLY for d in decisions)


def test_llm_usage_spike_low_ratio_observe_only(monkeypatch):
    """ratio < 5 and no session → single OBSERVE_ONLY (below governor threshold)."""
    from brain_loop import DecisionKind, _reflect_llm_usage_spike

    monkeypatch.setenv("BRAIN_LLM_COST_GOVERNOR", "on")
    o = _obs("llm_usage_spike", "global", {"ratio": 2})
    decisions = _reflect_llm_usage_spike(o, claude_session=None)
    assert len(decisions) == 1
    assert decisions[0].kind == DecisionKind.OBSERVE_ONLY


# ── _reflect_stale_atom ───────────────────────────────────────────────


def test_stale_atom_self_modifies_obsolete():
    from brain_loop import DecisionKind, _reflect_stale_atom

    o = _obs(
        "stale_atom",
        "atm_42",
        {
            "kind": "preference",
            "age_days": 200,
            "decay_threshold_days": 90,
            "reinforcement_count": 0,
            "text_preview": "Chris prefers X",
        },
    )
    d = _reflect_stale_atom(o, None)[0]
    assert d.kind == DecisionKind.SELF_MODIFY
    assert d.action_payload["modification"] == "atom_obsolete"
    assert d.action_payload["atom_id"] == "atm_42"
    assert d.action_payload["preview"] == "Chris prefers X"
    assert "200d" in d.action_payload["reason"]


# ── _reflect_proactive_urgent ─────────────────────────────────────────


def test_proactive_urgent_active_session_pushes_to_claude():
    from brain_loop import DecisionKind, _reflect_proactive_urgent

    o = _obs("proactive_urgent", "p1", {"summary": "S", "detail": "D"})
    d = _reflect_proactive_urgent(o, "sess_X")[0]
    assert d.kind == DecisionKind.PUSH_TO_CLAUDE
    assert d.action_payload["title"] == "S"
    assert d.action_payload["content"] == "D"
    assert d.action_payload["priority"] == "critical"


def test_proactive_urgent_no_session_dispatches_sage():
    from brain_loop import DecisionKind, _reflect_proactive_urgent

    o = _obs("proactive_urgent", "p1", {"summary": "S", "detail": "D"})
    d = _reflect_proactive_urgent(o, None)[0]
    assert d.kind == DecisionKind.DISPATCH_AGENT
    assert d.action_payload["agent"] == "sage"
    assert "Summary: S" in d.action_payload["message"]


# ── _reflect_proactive_playbook ───────────────────────────────────────


def test_proactive_playbook_active_session_pushes_to_claude():
    from brain_loop import DecisionKind, _reflect_proactive_playbook

    o = _obs(
        "proactive_playbook",
        "p1",
        {
            "summary": "S",
            "detail": "D",
            "event_class": "ec",
            "safe_actions": ["a1", "a2"],
            "stop_conditions": ["s1"],
        },
    )
    d = _reflect_proactive_playbook(o, "sess_X")[0]
    assert d.kind == DecisionKind.PUSH_TO_CLAUDE
    assert "a1" in d.action_payload["content"]
    assert "s1" in d.action_payload["content"]
    assert len(d.action_payload["content"]) <= 1800


def test_proactive_playbook_defaults_when_no_actions_or_stops():
    """Empty/missing safe_actions / stop_conditions → use the default
    placeholder lines so the body still reads sensibly."""
    from brain_loop import _reflect_proactive_playbook

    o = _obs("proactive_playbook", "p1", {"summary": "S", "detail": "D"})
    d = _reflect_proactive_playbook(o, "sess_X")[0]
    assert "gather read-only evidence" in d.action_payload["content"]
    assert "stop before destructive or credentialed work" in d.action_payload["content"]


def test_proactive_playbook_no_session_dispatches_sage():
    from brain_loop import DecisionKind, _reflect_proactive_playbook

    o = _obs("proactive_playbook", "p1", {"summary": "S", "detail": "D"})
    d = _reflect_proactive_playbook(o, None)[0]
    assert d.kind == DecisionKind.DISPATCH_AGENT
    assert d.action_payload["agent"] == "sage"


# ── _reflect_claude_active ────────────────────────────────────────────


def test_claude_active_returns_empty_decisions():
    """Side-channel signal, not a standalone decision."""
    from brain_loop import _reflect_claude_active

    o = _obs("claude_active", "sess_X")
    assert _reflect_claude_active(o, "sess_X") == []


# ── _reflect_llm_breaker_closed ───────────────────────────────────────


def test_llm_breaker_closed_drains_backlog():
    from brain_loop import DecisionKind, _reflect_llm_breaker_closed

    o = _obs("llm_breaker_closed", "llm.dispatch", {"pending_backlog": 42})
    d = _reflect_llm_breaker_closed(o, None)[0]
    assert d.kind == DecisionKind.SELF_MODIFY
    assert d.action_payload["modification"] == "drain_llm_backlog"
    assert d.action_payload["pending_backlog"] == 42


# ── _reflect_llm_backlog_breach ───────────────────────────────────────


def test_llm_backlog_overflow_is_urgent():
    from brain_loop import DecisionKind, _reflect_llm_backlog_breach

    o = _obs("llm_backlog_overflow", "backlog", {"pending": 50, "oldest_age_s": 7200})
    d = _reflect_llm_backlog_breach(o, None)[0]
    assert d.kind == DecisionKind.TELEGRAM_ALERT
    assert d.action_payload["severity"] == "urgent"
    assert "overflow" in d.action_payload["body"]
    assert "2.0h" in d.action_payload["body"]


def test_llm_backlog_stale_is_warn():
    from brain_loop import _reflect_llm_backlog_breach

    o = _obs("llm_backlog_stale", "backlog", {"pending": 10, "oldest_age_s": 3600})
    d = _reflect_llm_backlog_breach(o, None)[0]
    assert d.action_payload["severity"] == "warn"
    assert "stale" in d.action_payload["body"]


# ── _reflect_canonical_changed ────────────────────────────────────────


def test_canonical_changed_triggers_incremental_index():
    from brain_loop import DecisionKind, _reflect_canonical_changed

    o = _obs("canonical_changed", "fs_event", {"path": "canonical/_profile.md"})
    d = _reflect_canonical_changed(o, None)[0]
    assert d.kind == DecisionKind.SELF_MODIFY
    assert d.action_payload["modification"] == "incremental_canonical_index"
    assert d.confidence == 0.9


# ── _reflect (dispatcher) ─────────────────────────────────────────────


def test_reflect_dispatcher_routes_by_observation_kind(monkeypatch):
    """Each observation kind is routed through _REFLECT_HANDLERS;
    unknown kinds are silently dropped."""
    import brain_loop

    calls: list = []

    def _spy(name):
        def _h(o, sess):
            calls.append((name, o.kind, sess))
            return []

        return _h

    monkeypatch.setattr(
        brain_loop,
        "_REFLECT_HANDLERS",
        {"stalled_goal": _spy("g"), "recall_miss": _spy("rm")},
    )
    monkeypatch.setattr(brain_loop, "_find_active_claude_session", lambda obs: "sess_X")

    obs = [
        _obs("stalled_goal", "g1"),
        _obs("unknown_kind", "x"),  # should be dropped silently
        _obs("recall_miss", "s1"),
    ]
    brain_loop._reflect(obs)
    assert calls == [
        ("g", "stalled_goal", "sess_X"),
        ("rm", "recall_miss", "sess_X"),
    ]


def test_reflect_dispatcher_passes_claude_session_to_handlers(monkeypatch):
    """The session string returned by _find_active_claude_session is
    threaded to every handler invocation."""
    import brain_loop

    seen_sessions: list = []
    monkeypatch.setattr(
        brain_loop,
        "_REFLECT_HANDLERS",
        {"stalled_goal": lambda o, sess: seen_sessions.append(sess) or []},
    )
    monkeypatch.setattr(brain_loop, "_find_active_claude_session", lambda obs: "sess_42")

    brain_loop._reflect([_obs("stalled_goal", "g1"), _obs("stalled_goal", "g2")])
    assert seen_sessions == ["sess_42", "sess_42"]


def test_reflect_dispatcher_extends_results_from_multi_decision_handlers(monkeypatch):
    """When a handler returns >1 Decision (e.g. llm_usage_spike + governor),
    all of them must land in the final list."""
    import brain_loop
    from brain_loop import Decision, DecisionKind

    fake_decisions = [
        Decision(observation=_obs("k"), kind=DecisionKind.OBSERVE_ONLY),
        Decision(observation=_obs("k"), kind=DecisionKind.SELF_MODIFY),
    ]
    monkeypatch.setattr(
        brain_loop,
        "_REFLECT_HANDLERS",
        {"k": lambda o, sess: fake_decisions},
    )
    monkeypatch.setattr(brain_loop, "_find_active_claude_session", lambda obs: None)

    out = brain_loop._reflect([_obs("k", "s")])
    assert len(out) == 2
    assert out[0].kind == DecisionKind.OBSERVE_ONLY
    assert out[1].kind == DecisionKind.SELF_MODIFY


def test_reflect_dispatcher_all_real_kinds_registered():
    """The dispatch table must cover every kind the _sense_* layer emits.
    This guards against accidental table drift after a new sense type is
    added without a matching handler."""
    from brain_loop import _REFLECT_HANDLERS

    expected_kinds = {
        "stalled_goal",
        "recall_miss",
        "breaker_open",
        "accuracy_drop",
        "contradiction",
        "llm_usage_spike",
        "stale_atom",
        "proactive_urgent",
        "proactive_playbook",
        "claude_active",
        "llm_breaker_closed",
        "llm_backlog_overflow",
        "llm_backlog_stale",
        "canonical_changed",
    }
    assert expected_kinds <= set(_REFLECT_HANDLERS.keys())
