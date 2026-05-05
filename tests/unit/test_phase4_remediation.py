"""Phase 4: SLO direct remediation, cost governor, autonomy autograde tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core"))


# ── 4a: SLO direct remediations ──────────────────────────────────


def test_slo_direct_remediation_disabled_via_env(monkeypatch):
    monkeypatch.setenv("BRAIN_SLO_AUTOREMEDIATE", "off")
    import slo_monitor

    out = slo_monitor._apply_direct_remediations([{"slo": "outbox_pending_count", "current": 50}])
    assert out.get("skipped") == "disabled_via_env"


def test_slo_direct_remediation_outbox_triggers_drain(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAIN_SLO_AUTOREMEDIATE", "on")
    import slo_remediation

    monkeypatch.setattr(slo_remediation, "LOG_FILE", tmp_path / "slo_remediation.jsonl")
    monkeypatch.setattr(slo_remediation, "ESCALATION_LOG_FILE", tmp_path / "slo_escalations.jsonl")
    fake_jr = type(sys)("job_registry")
    fake_jr.dispatch_job = lambda name: 12345 if name == "outbox_drain" else 0
    monkeypatch.setitem(sys.modules, "job_registry", fake_jr)

    import slo_monitor

    out = slo_monitor._apply_direct_remediations([{"slo": "outbox_pending_count", "current": 50}])
    assert any(a.get("action") == "trigger:outbox_drain" for a in out["actions"])


def test_slo_direct_remediation_rss_sets_throttle_config(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAIN_SLO_AUTOREMEDIATE", "on")
    import slo_remediation

    monkeypatch.setattr(slo_remediation, "LOG_FILE", tmp_path / "slo_remediation.jsonl")
    monkeypatch.setattr(slo_remediation, "ESCALATION_LOG_FILE", tmp_path / "slo_escalations.jsonl")
    cfg: dict = {}
    fake_cfg = type(sys)("brain_config_store")
    fake_cfg.set = lambda k, v, **_kw: cfg.update({k: v})
    monkeypatch.setitem(sys.modules, "brain_config_store", fake_cfg)

    import slo_monitor

    slo_monitor._apply_direct_remediations([{"slo": "brain_server_rss_mb", "current": 2900}])
    assert cfg.get("BRAIN_SCHED_MAX_HEAVY_JOBS") == "0"
    assert "BRAIN_SCHED_HEAVY_THROTTLE_UNTIL" in cfg


def test_slo_direct_remediation_no_match_no_action(monkeypatch):
    monkeypatch.setenv("BRAIN_SLO_AUTOREMEDIATE", "on")
    import slo_monitor

    out = slo_monitor._apply_direct_remediations([{"slo": "some_other_slo", "current": 999}])
    assert out.get("actions") == []


# ── 4b: cost governor ────────────────────────────────────────────


def test_cli_llm_effective_concurrency_reads_override(monkeypatch):
    """When brain_config_store has BRAIN_CLI_LLM_CONCURRENCY=1 with valid
    UNTIL timestamp, cli_llm.effective_concurrency returns 1 instead of the
    env-var default of 2.
    """
    import time as _t

    fake_cfg = type(sys)("brain_config_store")
    overrides = {
        "BRAIN_CLI_LLM_CONCURRENCY": "1",
        "BRAIN_CLI_LLM_CONCURRENCY_UNTIL": str(int(_t.time() + 600)),
    }
    fake_cfg.get = lambda k: overrides.get(k)
    fake_cfg.set = lambda k, v, **_: overrides.update({k: v})
    monkeypatch.setitem(sys.modules, "brain_config_store", fake_cfg)

    import cli_llm

    # Reset cache so this test sees the override
    cli_llm._CONCURRENCY_OVERRIDE_CACHE = None
    assert cli_llm._effective_concurrency() == 1


def test_scheduler_applies_and_expires_slo_heavy_throttle(monkeypatch, tmp_path):
    import time as _t

    import scheduler

    cfg = {
        "BRAIN_SCHED_MAX_HEAVY_JOBS": "0",
        "BRAIN_SCHED_HEAVY_THROTTLE_UNTIL": str(int(_t.time() + 600)),
    }
    deleted: list[str] = []
    fake_cfg = type(sys)("brain_config_store")
    fake_cfg.get = lambda k: cfg.get(k)

    def _delete(key):
        deleted.append(key)
        cfg.pop(key, None)
        return True

    fake_cfg.delete = _delete
    monkeypatch.setitem(sys.modules, "brain_config_store", fake_cfg)

    sched = scheduler.BrainScheduler(db_path=tmp_path / "scheduler_history.db")
    sched._refresh_resource_limits_locked()

    assert sched._resource_limits["heavy"] == 0

    cfg["BRAIN_SCHED_HEAVY_THROTTLE_UNTIL"] = str(int(_t.time() - 1))
    sched._resource_limits_last_refresh = 0.0
    sched._refresh_resource_limits_locked()

    assert sched._resource_limits["heavy"] == sched._base_resource_limits["heavy"]
    assert "BRAIN_SCHED_HEAVY_THROTTLE_UNTIL" in deleted
    assert "BRAIN_SCHED_MAX_HEAVY_JOBS" in deleted


def test_cli_llm_effective_concurrency_expired_override_falls_back(monkeypatch):
    """Once BRAIN_CLI_LLM_CONCURRENCY_UNTIL is in the past, the override is
    ignored and we fall back to the env-default cap.
    """
    import time as _t

    fake_cfg = type(sys)("brain_config_store")
    overrides = {
        "BRAIN_CLI_LLM_CONCURRENCY": "1",
        "BRAIN_CLI_LLM_CONCURRENCY_UNTIL": str(int(_t.time() - 60)),  # expired
    }
    fake_cfg.get = lambda k: overrides.get(k)
    monkeypatch.setitem(sys.modules, "brain_config_store", fake_cfg)

    import cli_llm

    cli_llm._CONCURRENCY_OVERRIDE_CACHE = None
    monkeypatch.setattr(cli_llm, "MAX_CONCURRENT_CLI", 2)
    assert cli_llm._effective_concurrency() == 2


def test_brain_loop_cost_governor_engage_writes_config(monkeypatch):
    """SELF_MODIFY apply branch for engage_llm_cost_governor sets both keys."""
    import brain_loop

    cfg: dict = {}
    fake_cfg = type(sys)("brain_config_store")
    fake_cfg.set = lambda k, v, **_kw: cfg.update({k: v})
    monkeypatch.setitem(sys.modules, "brain_config_store", fake_cfg)

    ok = brain_loop._apply_self_modification(
        {
            "modification": "engage_llm_cost_governor",
            "ttl_s": 1800,
            "ratio": 6,
            "hourly": 600,
            "baseline": 100,
        }
    )
    assert ok is True
    assert cfg.get("BRAIN_CLI_LLM_CONCURRENCY") == "1"
    assert "BRAIN_CLI_LLM_CONCURRENCY_UNTIL" in cfg


def test_brain_loop_reflect_emits_governor_only_on_severe_spike():
    """ratio<5 should NOT engage governor (just alert); ratio>=5 with no
    Chris session SHOULD emit a SELF_MODIFY decision.
    """
    import brain_loop

    weak = brain_loop.Observation(
        kind="llm_usage_spike",
        subject="hourly",
        evidence={"hourly_rate": 200, "baseline_per_hour": 100, "ratio": 2.0},
    )
    decisions = brain_loop._reflect([weak])
    assert not any(
        d.kind == brain_loop.DecisionKind.SELF_MODIFY
        and d.action_payload.get("modification") == "engage_llm_cost_governor"
        for d in decisions
    )

    severe = brain_loop.Observation(
        kind="llm_usage_spike",
        subject="hourly",
        evidence={"hourly_rate": 600, "baseline_per_hour": 100, "ratio": 6.0},
    )
    decisions = brain_loop._reflect([severe])
    assert any(
        d.kind == brain_loop.DecisionKind.SELF_MODIFY
        and d.action_payload.get("modification") == "engage_llm_cost_governor"
        for d in decisions
    )


# ── 4c: autonomy autograde ───────────────────────────────────────


def test_autonomy_proposer_autograde_disabled_does_not_apply(monkeypatch):
    monkeypatch.setenv("BRAIN_AUTOGRADE_ENABLED", "off")
    import autonomy_proposer

    monkeypatch.setattr(
        autonomy_proposer,
        "_fetch_kind_outcomes",
        lambda: [{"kind": "heal.log_rotate", "total": 100, "success": 99}],
    )
    monkeypatch.setattr(autonomy_proposer, "_propose_audit", lambda *a, **k: None)
    fake_autonomy = type(sys)("autonomy")
    set_calls: list = []
    fake_autonomy.list_levels = lambda: {"heal.log_rotate": "L2"}
    fake_autonomy.set_level = lambda kind, level, **_kw: set_calls.append((kind, level))
    monkeypatch.setitem(sys.modules, "autonomy", fake_autonomy)

    out = autonomy_proposer.run()
    assert out["auto_applied_count"] == 0
    assert set_calls == []


def test_autonomy_proposer_autograde_applies_for_allowlisted_kind(monkeypatch):
    monkeypatch.setenv("BRAIN_AUTOGRADE_ENABLED", "on")
    import autonomy_proposer

    monkeypatch.setattr(
        autonomy_proposer,
        "_fetch_kind_outcomes",
        lambda: [{"kind": "heal.log_rotate", "total": 100, "success": 99}],
    )
    monkeypatch.setattr(autonomy_proposer, "_propose_audit", lambda *a, **k: None)

    # Mock brain_config_store so the cooldown lookup sees a fresh kind (no
    # last_promoted_at). Without this the test depends on real autonomy.db
    # state from prior runs.
    cfg_state: dict = {}
    fake_cfg = type(sys)("brain_config_store")
    fake_cfg.get = lambda k: cfg_state.get(k)
    fake_cfg.set = lambda k, v, **_kw: cfg_state.update({k: v})
    monkeypatch.setitem(sys.modules, "brain_config_store", fake_cfg)

    fake_autonomy = type(sys)("autonomy")
    set_calls: list = []
    fake_autonomy.list_levels = lambda: {"heal.log_rotate": "L2"}
    fake_autonomy.set_level = lambda kind, level, **_kw: set_calls.append((kind, level))
    monkeypatch.setitem(sys.modules, "autonomy", fake_autonomy)

    fake_tg = type(sys)("telegram_alert")
    fake_tg.send_chris_telegram = lambda **_kw: None
    monkeypatch.setitem(sys.modules, "telegram_alert", fake_tg)

    out = autonomy_proposer.run()
    assert out["auto_applied_count"] == 1
    assert set_calls == [("heal.log_rotate", "L3")]
    assert "autograde.heal.log_rotate.last_promoted_at" in cfg_state


def test_autonomy_proposer_autograde_skips_non_allowlisted(monkeypatch):
    monkeypatch.setenv("BRAIN_AUTOGRADE_ENABLED", "on")
    import autonomy_proposer

    monkeypatch.setattr(
        autonomy_proposer,
        "_fetch_kind_outcomes",
        lambda: [{"kind": "heal.reindex", "total": 100, "success": 99}],
    )
    monkeypatch.setattr(autonomy_proposer, "_propose_audit", lambda *a, **k: None)

    fake_autonomy = type(sys)("autonomy")
    set_calls: list = []
    fake_autonomy.list_levels = lambda: {"heal.reindex": "L2"}
    fake_autonomy.set_level = lambda kind, level, **_kw: set_calls.append((kind, level))
    monkeypatch.setitem(sys.modules, "autonomy", fake_autonomy)

    out = autonomy_proposer.run()
    assert out["auto_applied_count"] == 0
    assert set_calls == [], "non-allowlisted kind must NOT auto-apply even at 99% / 100 outcomes"


# ── Review fixes (2026-04-27): cooldown + flag-injection guard + dynamic kill ────


def test_autograde_cooldown_blocks_back_to_back_promotions(monkeypatch):
    """A kind that auto-promoted within AUTOGRADE_COOLDOWN_S must NOT be
    auto-promoted again. Prevents the L1->L2->L3 in-2-days escalation flagged
    by the code review.
    """
    import time as _t

    monkeypatch.setenv("BRAIN_AUTOGRADE_ENABLED", "on")
    import autonomy_proposer

    monkeypatch.setattr(
        autonomy_proposer,
        "_fetch_kind_outcomes",
        lambda: [{"kind": "heal.log_rotate", "total": 100, "success": 99}],
    )
    monkeypatch.setattr(autonomy_proposer, "_propose_audit", lambda *a, **k: None)

    # Cooldown: kind promoted just now → still hot
    fake_cfg = type(sys)("brain_config_store")
    cfg_state = {"autograde.heal.log_rotate.last_promoted_at": str(int(_t.time()))}
    fake_cfg.get = lambda k: cfg_state.get(k)
    fake_cfg.set = lambda k, v, **_kw: cfg_state.update({k: v})
    monkeypatch.setitem(sys.modules, "brain_config_store", fake_cfg)

    fake_autonomy = type(sys)("autonomy")
    set_calls: list = []
    fake_autonomy.list_levels = lambda: {"heal.log_rotate": "L2"}
    fake_autonomy.set_level = lambda kind, level, **_kw: set_calls.append((kind, level))
    monkeypatch.setitem(sys.modules, "autonomy", fake_autonomy)

    out = autonomy_proposer.run()
    assert set_calls == [], "cooldown should suppress back-to-back auto-promote"
    assert out["auto_applied_count"] == 0


def test_autograde_cooldown_expired_allows_promotion(monkeypatch):
    """After AUTOGRADE_COOLDOWN_S elapses, the same kind can be auto-promoted again."""
    import time as _t

    monkeypatch.setenv("BRAIN_AUTOGRADE_ENABLED", "on")
    import autonomy_proposer

    monkeypatch.setattr(
        autonomy_proposer,
        "_fetch_kind_outcomes",
        lambda: [{"kind": "heal.log_rotate", "total": 100, "success": 99}],
    )
    monkeypatch.setattr(autonomy_proposer, "_propose_audit", lambda *a, **k: None)

    cfg_state = {"autograde.heal.log_rotate.last_promoted_at": str(int(_t.time() - 30 * 24 * 3600))}
    fake_cfg = type(sys)("brain_config_store")
    fake_cfg.get = lambda k: cfg_state.get(k)
    fake_cfg.set = lambda k, v, **_kw: cfg_state.update({k: v})
    monkeypatch.setitem(sys.modules, "brain_config_store", fake_cfg)

    fake_autonomy = type(sys)("autonomy")
    set_calls: list = []
    fake_autonomy.list_levels = lambda: {"heal.log_rotate": "L2"}
    fake_autonomy.set_level = lambda kind, level, **_kw: set_calls.append((kind, level))
    monkeypatch.setitem(sys.modules, "autonomy", fake_autonomy)

    fake_tg = type(sys)("telegram_alert")
    fake_tg.send_chris_telegram = lambda **_kw: None
    monkeypatch.setitem(sys.modules, "telegram_alert", fake_tg)

    out = autonomy_proposer.run()
    assert set_calls == [("heal.log_rotate", "L3")]
    assert out["auto_applied_count"] == 1
    assert "autograde.heal.log_rotate.last_promoted_at" in cfg_state


def test_brain_command_rejects_dash_prefixed_content_for_outbox_targets(monkeypatch, tmp_path):
    """A spawn-target task starting with '-' could be parsed as a CLI flag by
    claude/codex. Reject at the API boundary.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core" / "routes"))
    if "command" in sys.modules:
        del sys.modules["command"]
    import command

    monkeypatch.setattr(command, "_OUTBOX_ROOT", tmp_path)
    fake_messenger = type(sys)("agent_messenger")
    fake_messenger.send_message = lambda **kw: {"id": "x", "created_at": "now", "_action": "stored"}
    monkeypatch.setitem(sys.modules, "agent_messenger", fake_messenger)
    fake_atoms = type(sys)("atoms_store")
    fake_atoms.insert_raw_event = lambda **_kw: None
    monkeypatch.setitem(sys.modules, "atoms_store", fake_atoms)

    from fastapi import HTTPException

    bad = command.BrainCommandRequest(to_agent="claude", content="-rm rf /")
    with pytest.raises(HTTPException) as exc_info:
        command.brain_command(bad)
    assert exc_info.value.status_code == 400
    assert "flag-injection" in exc_info.value.detail.lower()

    # Same content to a non-outbox target is fine
    fine = command.BrainCommandRequest(to_agent="jenna", content="-rm rf /")
    out = command.brain_command(fine)
    assert out["ok"] is True


def test_search_unified_freshness_kill_switch_is_dynamic(monkeypatch):
    """BRAIN_TRUST_FRESHNESS=off should take effect WITHOUT restart since the
    review fix made it dynamic.
    """
    import search_unified

    # Force-on path: very old canonical decays to floor 0.8
    monkeypatch.setenv("BRAIN_TRUST_FRESHNESS", "on")
    now = 1_745_000_000.0
    very_old = now - (1000 * 86400)
    assert search_unified._effective_trust("canonical", very_old, _now_ts=now) == 0.8

    # Flip kill switch in the SAME process — no restart
    monkeypatch.setenv("BRAIN_TRUST_FRESHNESS", "off")
    assert search_unified._effective_trust("canonical", very_old, _now_ts=now) == 1.0
