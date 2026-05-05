from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core"))

from routes import think


def test_chris_think_uses_cli_first_dispatch(monkeypatch):
    calls = []

    monkeypatch.setattr(
        think,
        "_compose_think_prompt",
        lambda question, extra_context: ("think prompt", []),
    )
    monkeypatch.setattr(
        think._metrics_buf,
        "record_dispatch",
        lambda **kwargs: None,
    )

    def fake_dispatch(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            ok=True,
            text="I should keep Brain work CLI-first.",
            duration_ms=12,
            rate_limited=False,
            auth_failed=False,
            attempts=1,
            model="gpt-5.5",
        )

    monkeypatch.setattr(think, "_llm_dispatch", fake_dispatch)
    think._think_cache.clear()

    response = think.chris_think(
        think.ThinkRequest(question="What should I do?", context="unit test"),
        background=None,
    )

    assert response.answer == "I should keep Brain work CLI-first."
    assert response.model == "gpt-5.5"
    assert calls
    call = calls[0]
    assert call["agent"] == "jenna"
    assert call["openclaw_agent"] == "jenna"
    assert call["backlog_kind"] == "synthesis"
    assert call["backlog_payload"]["source"] == "routes.think"
    assert "backend" not in call
    assert "max_backends" not in call
