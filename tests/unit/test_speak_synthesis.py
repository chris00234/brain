from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core"))

import speak_synthesis


def test_synthesis_observation_dedup_key_is_stable_for_openclaw_no_response_variants():
    parsed = {
        "observations": [
            {
                "severity": 7.6,
                "category": "pattern",
                "message": "OpenClaw 업데이트 이후 무응답 점검이 여러 세션에서 반복돼서 문제 재현과 원인 격리가 아직 끝나지 않았다.",
            },
            {
                "severity": 7.6,
                "category": "pattern",
                "message": "OpenClaw 업데이트 후 무응답 문제가 반복되는데 원인 탐색이 codex_boot와 liveness로 퍼져 있다.",
            },
        ]
    }

    observations = speak_synthesis._emit_observations(parsed)

    assert len(observations) == 2
    assert {o.dedup_key for o in observations} == {"synth:openclaw_no_response_regression"}


def test_synthesis_command_dedup_key_is_stable_for_openclaw_no_response_variants(monkeypatch):
    monkeypatch.setattr(speak_synthesis, "was_sent_recently", lambda *args, **kwargs: False)
    monkeypatch.setattr(speak_synthesis, "_synthesis_auto_dispatch_enabled", lambda: False)
    parsed = {
        "commands": [
            {
                "to_agent": "liz",
                "content": "OpenClaw 업데이트 후 무응답 현상을 재현하고 codex_boot.sh와 liveness.py 기준으로 원인을 좁혀.",
                "reason": "반복 운영 이슈",
                "priority": 2,
            }
        ]
    }

    observations = speak_synthesis._emit_commands(parsed)

    assert len(observations) == 1
    assert observations[0].dedup_key == "cmd:openclaw_no_response_regression"
