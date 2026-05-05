"""Unit tests for learned proactive playbook detection."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))

from proactive_playbooks import (  # noqa: E402
    RecentEvent,
    detect_playbook_candidates,
    discover_dynamic_playbooks,
    load_dynamic_playbooks,
)


def test_detects_software_update_playbook_without_llm_or_search():
    events = [
        RecentEvent(
            id="evt-1",
            timestamp="2026-04-30T20:00:00+00:00",
            source_type="shell",
            actor="chris",
            content="openclaw update completed: OpenClaw 2026.4.29",
        )
    ]

    candidates = detect_playbook_candidates(events=events, include_learned_evidence=False)

    update = next(c for c in candidates if c.event_class == "software_update")
    assert update.severity == "info"
    assert "previous and current version" in " ".join(update.safe_actions)
    assert "destructive" in " ".join(update.stop_conditions)
    assert update.evidence[0]["safe_mode"] == "read_only_or_advisory"


def test_detects_restart_playbook_as_warning():
    events = [
        RecentEvent(
            id="evt-2",
            timestamp="2026-04-30T20:05:00+00:00",
            source_type="shell",
            actor="chris",
            content="openclaw gateway restart && openclaw status",
        )
    ]

    candidates = detect_playbook_candidates(events=events, include_learned_evidence=False)

    restart = next(c for c in candidates if c.event_class == "service_restart")
    assert restart.severity == "warning"
    assert "status/process/listener" in " ".join(restart.safe_actions)


def test_detects_proactive_miss_feedback_playbook():
    events = [
        RecentEvent(
            id="evt-miss-1",
            timestamp="2026-04-30T20:08:00+00:00",
            source_type="openclaw_session",
            actor="chris",
            content="이런 건 내가 물어보기 전에 먼저 알려줘야 하는거 아니야?",
        )
    ]

    candidates = detect_playbook_candidates(events=events, include_learned_evidence=False)

    miss = next(c for c in candidates if c.event_class == "proactive_miss_feedback")
    assert miss.severity == "warning"
    assert "missed proactive opportunity" in " ".join(miss.safe_actions)
    assert "overfit" in " ".join(miss.stop_conditions)


def test_detects_background_job_status_playbook():
    events = [
        RecentEvent(
            id="evt-3",
            timestamp="2026-04-30T20:10:00+00:00",
            source_type="openclaw_session",
            actor="chris",
            content="오케이 그럼 이제 백그라운드에서 도는 잡들은 어떻게 되어가?",
        ),
        RecentEvent(
            id="evt-4",
            timestamp="2026-04-30T20:11:00+00:00",
            source_type="shell",
            actor="system",
            content="scheduler job failed twice; queue drain backlog pending",
        ),
    ]

    candidates = detect_playbook_candidates(events=events, include_learned_evidence=False)

    jobs = next(c for c in candidates if c.event_class == "background_job_status")
    assert jobs.severity == "warning"
    assert "last run" in " ".join(jobs.safe_actions)
    assert "heavy catch-up jobs" in " ".join(jobs.stop_conditions)


def test_detects_quota_and_backup_playbooks():
    events = [
        RecentEvent(
            id="evt-5",
            timestamp="2026-04-30T20:12:00+00:00",
            source_type="shell",
            actor="system",
            content="ERROR usage limit reached; llm usage rate limit active",
        ),
        RecentEvent(
            id="evt-6",
            timestamp="2026-04-30T20:13:00+00:00",
            source_type="shell",
            actor="system",
            content="qdrant backup snapshot created",
        ),
    ]

    candidates = detect_playbook_candidates(events=events, include_learned_evidence=False)
    classes = {c.event_class for c in candidates}

    assert "quota_or_usage_pressure" in classes
    assert "backup_or_restore" in classes


def test_detects_oauth_infra_ui_and_code_verification_playbooks():
    events = [
        RecentEvent(
            id="evt-7",
            timestamp="2026-04-30T20:14:00+00:00",
            source_type="shell",
            actor="chris",
            content="oauth callback config changed and token refresh route patched",
        ),
        RecentEvent(
            id="evt-8",
            timestamp="2026-04-30T20:15:00+00:00",
            source_type="shell",
            actor="chris",
            content="docker compose nginx cloudflare tunnel port config updated",
        ),
        RecentEvent(
            id="evt-9",
            timestamp="2026-04-30T20:16:00+00:00",
            source_type="coding_event",
            actor="codex",
            content="frontend dashboard css layout patched; screenshot needed",
        ),
        RecentEvent(
            id="evt-10",
            timestamp="2026-04-30T20:17:00+00:00",
            source_type="coding_event",
            actor="codex",
            content="code change edited refactor; pytest and ruff verification needed",
        ),
    ]

    candidates = detect_playbook_candidates(events=events, include_learned_evidence=False)
    classes = {c.event_class for c in candidates}

    assert "oauth_or_auth_flow_change" in classes
    assert "infra_edge_change" in classes
    assert "ui_or_visual_change" in classes
    assert "code_change_verification" in classes


def test_discovers_dynamic_playbook_without_source_update(tmp_path, monkeypatch):
    import proactive_playbooks

    monkeypatch.setattr(proactive_playbooks, "DYNAMIC_PLAYBOOKS_FILE", tmp_path / "classes.json")
    events = [
        RecentEvent(
            id=f"dyn-{i}",
            timestamp=f"2026-04-30T20:{20 + i:02d}:00+00:00",
            source_type="openclaw_session",
            actor="chris",
            content="graph dreamliner autopruner 상태를 먼저 확인해서 알려줘",
        )
        for i in range(3)
    ]

    discovered = discover_dynamic_playbooks(events=events, persist=True)

    assert len(discovered) == 1
    assert discovered[0].dynamic is True
    assert discovered[0].support == 3
    assert discovered[0].min_keyword_matches == 2
    assert load_dynamic_playbooks()[0].name == discovered[0].name


def test_dynamic_playbook_matches_future_event_without_code_change(tmp_path, monkeypatch):
    import proactive_playbooks

    monkeypatch.setattr(proactive_playbooks, "DYNAMIC_PLAYBOOKS_FILE", tmp_path / "classes.json")
    training_events = [
        RecentEvent(
            id=f"learn-{i}",
            timestamp=f"2026-04-30T20:{30 + i:02d}:00+00:00",
            source_type="openclaw_session",
            actor="chris",
            content="qmd loomwatch nursery 상태를 먼저 확인해서 알려줘",
        )
        for i in range(3)
    ]
    discover_dynamic_playbooks(events=training_events, persist=True)

    future_events = [
        RecentEvent(
            id="future-1",
            timestamp="2026-04-30T21:00:00+00:00",
            source_type="openclaw_session",
            actor="chris",
            content="qmd loomwatch nursery 지금 어떻게 되어가?",
        )
    ]
    candidates = detect_playbook_candidates(
        events=future_events,
        include_learned_evidence=False,
        include_dynamic_discovery=False,
    )

    assert len(candidates) == 1
    assert candidates[0].event_class.startswith("learned_")
    assert "generic" not in candidates[0].summary.lower()
    assert "read-only" in " ".join(candidates[0].safe_actions)


def test_brain_loop_reflect_turns_playbook_into_active_session_doorbell():
    import brain_loop

    observations = [
        brain_loop.Observation(kind="claude_active", subject="sess-1"),
        brain_loop.Observation(
            kind="proactive_playbook",
            subject="software_update:abc",
            evidence={
                "summary": "post-update change review after recent software update signal",
                "detail": "Detected a recent software_update event class.",
                "event_class": "software_update",
                "safe_actions": ["identify previous and current version"],
                "stop_conditions": ["do not install or restart without request"],
            },
        ),
    ]

    decisions = brain_loop._reflect(observations)

    decision = next(d for d in decisions if d.observation.kind == "proactive_playbook")
    assert decision.kind == brain_loop.DecisionKind.PUSH_TO_CLAUDE
    assert decision.requires_autonomy == "brain_loop.proactive_playbook_execute"
    assert "Execute only the safe layer" in decision.action_payload["content"]


def test_e2e_dry_run_candidate_reaches_brain_loop_doorbell(monkeypatch):
    import brain_loop

    candidates = detect_playbook_candidates(
        events=[
            RecentEvent(
                id="evt-e2e-1",
                timestamp="2026-04-30T21:10:00+00:00",
                source_type="openclaw_session",
                actor="chris",
                content="이런 건 내가 물어보기 전에 먼저 알려줘야 하는거 아니야?",
            )
        ],
        include_learned_evidence=False,
    )
    candidate = next(c for c in candidates if c.event_class == "proactive_miss_feedback")
    insight = SimpleNamespace(
        id="dry-run-proactive-miss",
        category="playbook",
        severity=candidate.severity,
        summary=candidate.summary,
        detail=candidate.detail,
        evidence=list(candidate.evidence),
    )
    monkeypatch.setattr(brain_loop, "get_current_insights", lambda max_age_hours=6: [insight])

    observations = [
        brain_loop.Observation(kind="claude_active", subject="sess-dry-run"),
        *brain_loop._sense_proactive_playbooks(),
    ]
    decisions = brain_loop._reflect(observations)

    decision = next(d for d in decisions if d.observation.kind == "proactive_playbook")
    assert decision.kind == brain_loop.DecisionKind.PUSH_TO_CLAUDE
    assert decision.action_payload["session_id"] == "sess-dry-run"
    assert decision.action_payload["priority"] == "high"
    assert "proactive_miss_feedback" in decision.action_payload["content"]
