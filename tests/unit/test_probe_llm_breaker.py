from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

BRAIN_ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "probe_llm_breaker", BRAIN_ROOT / "cli" / "probe_llm_breaker.py"
)
probe = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(probe)


def _snap(state: str = "open"):
    return SimpleNamespace(
        kind=probe.BREAKER_KIND,
        state=state,
        failures=3,
        trip_count=1,
        reset_after_s=300,
        remaining_cooldown_s=123.4,
        reason="test",
        opened_at=1.0,
        last_failure_at=2.0,
        last_action_at=3.0,
    )


def test_probe_closes_breaker_after_success(monkeypatch):
    monkeypatch.setattr(probe, "FALLBACK_CHAIN", [("codex", "gpt-test", "test")])
    monkeypatch.setattr(probe, "peek_breaker", lambda kind: _snap())
    monkeypatch.setattr(
        probe,
        "_try_backend",
        lambda backend, model, prompt, timeout: SimpleNamespace(ok=True, text="OK", duration_ms=7, error=""),
    )
    recorded = {}

    def fake_record(kind, *, ok, error=""):
        recorded.update({"kind": kind, "ok": ok, "error": error})
        return _snap("closed")

    monkeypatch.setattr(probe, "record_result", fake_record)

    out = probe.run(timeout=1, max_backends=1)

    assert out["ok"] is True
    assert recorded == {"kind": probe.BREAKER_KIND, "ok": True, "error": ""}
    assert out["after_reset"]["state"] == "closed"


def test_probe_failure_does_not_extend_breaker(monkeypatch):
    monkeypatch.setattr(probe, "FALLBACK_CHAIN", [("codex", "gpt-test", "test")])
    monkeypatch.setattr(probe, "peek_breaker", lambda kind: _snap())
    monkeypatch.setattr(
        probe,
        "_try_backend",
        lambda backend, model, prompt, timeout: SimpleNamespace(
            ok=False, text="", duration_ms=7, error="nope"
        ),
    )

    def fail_record(*args, **kwargs):  # pragma: no cover - should not be called
        raise AssertionError("record_result should not run on failed probe")

    monkeypatch.setattr(probe, "record_result", fail_record)

    out = probe.run(timeout=1, max_backends=1)

    assert out["ok"] is False
    assert out["note"] == "probe_failed_breaker_not_extended"
