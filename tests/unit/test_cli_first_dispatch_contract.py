"""Regression tests for CLI-first background LLM and alert dispatch."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))
sys.path.insert(0, str(BRAIN_ROOT / "ingest"))
sys.path.insert(0, str(BRAIN_ROOT / "cli"))


def test_cli_llm_default_chain_starts_with_gpt55_and_keeps_openclaw_last():
    import cli_llm

    first_backend, first_model, _ = cli_llm.FALLBACK_CHAIN[0]
    last_backend, _, _ = cli_llm.FALLBACK_CHAIN[-1]

    assert (first_backend, first_model) == ("codex", "gpt-5.5")
    assert cli_llm.FALLBACK_CHAIN[1][:2] == ("codex", "gpt-5.3-codex-spark")
    assert all(backend != "claude" for backend, _model, _desc in cli_llm.FALLBACK_CHAIN)
    assert not hasattr(cli_llm, "CLAUDE_BIN")
    assert last_backend == "openclaw"


def test_ingest_adapters_use_shared_cli_first_dispatch_not_openclaw_shellout():
    adapters = [
        "screen_time.py",
        "git_activity.py",
        "active_contacts.py",
        "gmail.py",
        "claude_code_sessions.py",
        "browser.py",
        "openclaw_sessions.py",
    ]
    for name in adapters:
        text = (BRAIN_ROOT / "ingest" / name).read_text()
        assert "from llm_dispatch import dispatch_json" in text
        assert "OPENCLAW_BIN" not in text
        assert "openclaw agent --agent" not in text.lower()
        assert '"--agent"' not in text


def test_ingest_dispatch_json_uses_cli_llm_default_chain(monkeypatch):
    import llm_dispatch

    calls: list[dict] = []

    def fake_dispatch(agent, message, **kwargs):
        calls.append({"agent": agent, "message": message, **kwargs})
        return SimpleNamespace(ok=True, text='```json\n{"keep": []}\n```')

    monkeypatch.setitem(sys.modules, "cli_llm", SimpleNamespace(dispatch=fake_dispatch))

    assert llm_dispatch.dispatch_json(
        agent="sage",
        prompt="return json",
        timeout=9,
        source="ingest.test",
        thinking="off",
    ) == {"keep": []}
    assert calls == [
        {
            "agent": "sage",
            "message": "return json",
            "thinking": "off",
            "timeout": 9,
            "openclaw_agent": "sage",
            "backlog_kind": "distill",
            "backlog_payload": {"source": "ingest.test", "agent": "sage", "prompt": "return json"},
        }
    ]
    assert "backend" not in calls[0]
    assert "max_backends" not in calls[0]
    assert "openclaw_session_id" not in calls[0]


def test_hyde_uses_central_dispatch_without_direct_openclaw(monkeypatch):
    if "hyde" in sys.modules:
        del sys.modules["hyde"]
    import hyde

    calls: list[dict] = []

    def fake_dispatch(agent, message, **kwargs):
        calls.append({"agent": agent, "message": message, **kwargs})
        return SimpleNamespace(ok=True, text="hypothetical answer")

    monkeypatch.setitem(sys.modules, "cli_llm", SimpleNamespace(dispatch=fake_dispatch))

    assert hyde._dispatch_to_jenna("prompt", thinking="low", timeout=11) == "hypothetical answer"
    assert calls[0]["agent"] == "jenna"
    assert calls[0]["timeout"] == 11
    assert calls[0]["openclaw_agent"] == "jenna"
    assert calls[0]["backlog_kind"] == "synthesis"
    assert "backend" not in calls[0]
    assert "max_backends" not in calls[0]
    assert "openclaw_session_id" not in calls[0]

    source = (BRAIN_ROOT / "brain_core" / "hyde.py").read_text()
    assert "from openclaw_dispatch" not in source
    assert "OPENCLAW_BIN" not in source


def test_lora_and_healthcheck_alerts_use_direct_telegram(monkeypatch):
    calls: list[dict] = []

    def fake_send(message, source, severity, **_kwargs):
        calls.append({"message": message, "source": source, "severity": severity})
        return True

    monkeypatch.setitem(sys.modules, "telegram_alert", SimpleNamespace(send_chris_telegram=fake_send))

    if "lora_ab_gate" in sys.modules:
        del sys.modules["lora_ab_gate"]
    import lora_ab_gate

    lora_ab_gate._alert("Rejected", "delta too low")

    if "healthcheck" in sys.modules:
        del sys.modules["healthcheck"]
    healthcheck = importlib.import_module("healthcheck")
    assert healthcheck.send_telegram("health bad") is True

    assert calls == [
        {
            "message": "[BRAIN LoRA A/B] Rejected\ndelta too low",
            "source": "lora_ab_gate",
            "severity": "warn",
        },
        {"message": "health bad", "source": "ingest.healthcheck", "severity": "warn"},
    ]


def test_server_watchdog_alert_path_is_llm_free():
    text = (BRAIN_ROOT / "cli" / "server_watchdog.sh").read_text()

    assert "OPENCLAW_BIN" not in text
    assert "agent --agent" not in text
    assert "telegram_alert" in text
    assert "send_chris_telegram" in text


def test_cli_llm_usage_stats_reports_current_cli_surface(monkeypatch, tmp_path):
    import cli_llm

    usage_db = tmp_path / "llm_usage.db"
    monkeypatch.setattr(cli_llm, "LLM_USAGE_DB", usage_db)

    cli_llm._record_usage("codex", "gpt-5.5", tokens=123, duration_ms=45, ok=True)
    cli_llm._record_usage("openclaw", "jenna", tokens=7, duration_ms=10, ok=False, rate_limited=True)

    stats = cli_llm.get_usage_stats(days=1)

    assert stats["source"] == "cli_llm"
    assert stats["primary_model"] == "gpt-5.5"
    assert stats["total"] == 1
    assert stats["cb_skipped"] == 1
    assert stats["per_agent"] == {"cli:codex": 1}
    assert stats["per_backend"] == {"codex": 1}
    assert stats["tokens"] == 130


def test_brain_loop_and_usage_route_do_not_import_openclaw_dispatch_directly():
    brain_loop = (BRAIN_ROOT / "brain_core" / "brain_loop.py").read_text()
    brain_ops = (BRAIN_ROOT / "brain_core" / "routes" / "brain_ops.py").read_text()

    assert "from openclaw_dispatch import dispatch" not in brain_loop
    assert "_openclaw_dispatch" not in brain_loop
    assert "import openclaw_dispatch" not in brain_ops
    assert "cli_llm.get_usage_stats" in brain_ops


def test_user_facing_docs_state_cli_first_usage_and_task_eval_contract():
    docs = {
        "README.md": (BRAIN_ROOT / "README.md").read_text(),
        "AGENT_HARNESS.md": (BRAIN_ROOT / "AGENT_HARNESS.md").read_text(),
        "brain/ARCHITECTURE.md": (BRAIN_ROOT / "brain" / "ARCHITECTURE.md").read_text(),
    }

    for name, text in docs.items():
        assert "CLI-first" in text or "cli_llm" in text, name
        assert "gpt-5.5" in text, name
        assert "/brain/usage" in text, name
        assert "source=cli_llm" in text or "llm.source=cli_llm" in text, name
        assert "primary_model=gpt-5.5" in text or "llm.primary_model=gpt-5.5" in text, name

    combined = "\n".join(docs.values())
    assert "task_queue:evaluation_action_summary" in combined
    assert "TASK EVALUATION ACTION" in combined
    assert "not a\nrequest" in combined or "not as requests" in combined or "not a request" in combined
