from __future__ import annotations

import sys
import threading
from pathlib import Path

HERMES_ROOT = Path.home() / ".hermes/hermes-agent"
if str(HERMES_ROOT) not in sys.path:
    sys.path.insert(0, str(HERMES_ROOT))

from hermes_integration import brain_memory_provider as provider_mod  # noqa: E402
from hermes_integration.brain_memory_provider import BrainMemoryProvider  # noqa: E402


def test_shutdown_drains_queued_turn_writes(monkeypatch):
    writes: list[dict] = []

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        if path == "/memory" and method == "POST":
            writes.append({"body": body, "actor": actor})
        return {"ok": True}

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)

    provider = BrainMemoryProvider()
    provider._profile = "jenna"
    provider._writer_thread = threading.Thread(target=provider._writer_loop)
    provider._writer_thread.start()

    provider.sync_turn("u1", "a1", session_id="s1")
    provider.sync_turn("u2", "a2", session_id="s1")
    provider.sync_turn("u3", "a3", session_id="s1")
    provider.shutdown()

    assert [w["body"]["content"] for w in writes] == [
        "User: u1\nAssistant: a1",
        "User: u2\nAssistant: a2",
        "User: u3\nAssistant: a3",
    ]
    assert {w["actor"] for w in writes} == {"jenna"}


def test_prefetch_uses_profile_scoped_recall(monkeypatch):
    calls: list[dict] = []

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        calls.append({"path": path, "method": method, "actor": actor})
        return {
            "results": [
                {
                    "title": "Preference",
                    "content": "Chris prefers concise Korean status updates.",
                    "score": 0.91,
                }
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)

    provider = BrainMemoryProvider()
    provider._profile = "sage"
    context = provider.prefetch("response style")

    assert "Brain recall (profile=sage)" in context
    assert "Chris prefers concise Korean status updates." in context
    assert calls[0]["method"] == "GET"
    assert calls[0]["actor"] == "sage"
    assert "/recall/v2?" in calls[0]["path"]
    assert "agent=sage" in calls[0]["path"]


def test_builtin_memory_write_is_mirrored_to_brain(monkeypatch):
    writes: list[dict] = []

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        if path == "/memory" and method == "POST":
            writes.append({"body": body, "actor": actor})
        return {"ok": True}

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)

    provider = BrainMemoryProvider()
    provider._profile = "liz"
    provider._platform = "cli"
    provider._writer_thread = threading.Thread(target=provider._writer_loop)
    provider._writer_thread.start()

    provider.on_memory_write(
        "add",
        "user",
        "Chris prefers durable settings to be explicit.",
        metadata={"session_id": "s2"},
    )
    provider.shutdown()

    assert writes == [
        {
            "actor": "liz",
            "body": {
                "content": "Chris prefers durable settings to be explicit.",
                "category": "preference",
                "agent": "liz",
                "source": "hermes",
                "confidence": 0.65,
                "reason": ("kind=builtin_memory_write action=add target=user " "session=s2 platform=cli"),
            },
        }
    ]
