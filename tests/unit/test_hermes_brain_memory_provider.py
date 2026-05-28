from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
HERMES_ROOT_CANDIDATES = [
    Path(p)
    for p in (
        os.environ.get("HERMES_AGENT_ROOT", ""),
        os.environ.get("OMX_ADAPT_HERMES_ROOT", ""),
        "/Users/chrischo/.hermes/hermes-agent",
        str(Path.home() / ".hermes/hermes-agent"),
        str(BRAIN_ROOT.parent / "hermes-agent"),
    )
    if p
]
for hermes_root in HERMES_ROOT_CANDIDATES:
    if (hermes_root / "agent" / "memory_provider.py").exists():
        sys.path.insert(0, str(hermes_root))
        break

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


def test_prefetch_collapses_near_duplicate_eval_score_preferences(monkeypatch):
    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        return {
            "results": [
                {
                    "id": "old",
                    "collection": "semantic_memory",
                    "title": "Brain eval preference",
                    "content": "Chris wants Brain fine tuning judged by measurable eval score improvements.",
                    "score": 0.99,
                },
                {
                    "id": "canonical",
                    "collection": "canonical",
                    "title": "Brain quality decision",
                    "content": "Brain fine-tuning should improve measurable eval score improvements, not vibes.",
                    "score": 0.70,
                },
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)

    provider = BrainMemoryProvider()
    provider._profile = "liz"
    context = provider.prefetch("브레인 검색품질 평가 점수 개선")

    assert context.count("eval score") == 1
    assert "canonical: Brain quality decision" in context


def test_prefetch_filters_generic_brain_infra_noise(monkeypatch):
    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        return {
            "results": [
                {
                    "id": "actionable",
                    "title": "Brain eval score preference",
                    "collection": "canonical",
                    "content": "Chris wants Brain fine-tuning judged by measurable eval score improvements, not vibes.",
                    "score": 0.97,
                },
                {
                    "id": "noise",
                    "title": "Knowledge gap bridge: Brain system dependency",
                    "collection": "canonical",
                    "content": "Brain depends on FastAPI brain-server, native Qdrant, and native Ollama.",
                    "score": 0.95,
                },
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    context = provider.prefetch("Brain recall quality should be no-noise and improve eval score")

    assert "Brain recall" in context
    assert "eval score improvements" in context
    assert "Knowledge gap bridge" not in context


def test_prefetch_returns_empty_when_all_brain_quality_hits_are_noise(monkeypatch):
    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        return {
            "results": [
                {
                    "id": "noise",
                    "title": "Knowledge gap bridge: Brain system dependency",
                    "collection": "canonical",
                    "content": "Brain depends on FastAPI brain-server, native Qdrant, and native Ollama.",
                    "score": 0.95,
                }
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    assert provider.prefetch("Brain recall quality should be no-noise") == ""


def test_prefetch_korean_live_status_prompt_suppresses_stale_memory(monkeypatch):
    calls: list[str] = []

    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        calls.append(path)
        return {"results": [{"title": "Old kanban memory", "content": "stale", "score": 1.0}]}

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    assert provider.prefetch("칸반 태스크 t_41c206ec 진행상황 업데이트") == ""
    assert calls == []


def test_prefetch_capability_recommendation_keeps_hard_constraints(monkeypatch):
    def fake_request(path, method="GET", body=None, timeout=5.0, actor=None):
        return {
            "results": [
                {
                    "id": "constraint",
                    "title": "Music/TTS capability constraint",
                    "collection": "canonical",
                    "content": "For music/TTS capability recommendations, Chris has hard constraints against local generation models and paid SaaS API billing.",
                    "score": 0.8,
                },
                {
                    "id": "noise",
                    "title": "Boston session note",
                    "collection": "experience",
                    "content": "A trip note that mentioned audio once.",
                    "score": 0.99,
                },
            ]
        }

    monkeypatch.setattr(provider_mod, "_brain_request", fake_request)
    provider = BrainMemoryProvider()

    context = provider.prefetch("Get me updated recommendations for music/TTS capability")

    assert "hard constraints against local generation models" in context
    assert "Boston session note" not in context
