"""Phase 2: lesson_fanout user-hook unit tests.

Verifies that the on_memory_stored handler in ~/.brain_hooks/lesson_fanout.py
correctly classifies lesson-worthy atoms, routes by tag, broadcasts
corrections, and respects the BRAIN_LESSON_FANOUT kill switch.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

HOOK_PATH = Path.home() / ".brain_hooks" / "lesson_fanout.py"


def _load_hook(monkeypatch=None):
    """Load the user hook as a module. Tests can monkeypatch its module
    namespace before calling on_memory_stored.
    """
    if not HOOK_PATH.exists():
        pytest.skip(f"hook file missing: {HOOK_PATH}")
    spec = importlib.util.spec_from_file_location("lesson_fanout", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lesson_fanout"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_classify_correction_broadcasts():
    mod = _load_hook()
    is_lesson, targets = mod._classify_lesson(kind="correction", confidence=0.4, text="don't do X")
    assert is_lesson is True
    assert "claude" in targets and "codex" in targets


def test_classify_high_confidence_preference_with_tag_routes():
    mod = _load_hook()
    is_lesson, targets = mod._classify_lesson(
        kind="preference",
        confidence=0.9,
        text="prefer Docker on OrbStack for homelab infra",
    )
    assert is_lesson is True
    # ellie owns infra; should be in targets
    assert "ellie" in targets


def test_classify_low_confidence_fact_skipped():
    mod = _load_hook()
    is_lesson, _ = mod._classify_lesson(kind="fact", confidence=0.5, text="brain runs on port 8791")
    assert is_lesson is False


def test_classify_lesson_marker_with_high_confidence():
    mod = _load_hook()
    is_lesson, targets = mod._classify_lesson(
        kind="fact",
        confidence=0.9,
        text="always run typecheck before commit",
    )
    assert is_lesson is True
    # "code" tag in text → claude+codex
    assert "claude" in targets


def test_route_by_tag_infra_routes_ellie():
    mod = _load_hook()
    targets = mod._route_by_tag("nginx config update for homelab")
    assert "ellie" in targets


def test_route_by_tag_no_match_returns_empty():
    mod = _load_hook()
    targets = mod._route_by_tag("today's weather is nice")
    assert targets == ()


def test_on_memory_stored_disabled_does_nothing(monkeypatch):
    mod = _load_hook()
    monkeypatch.setattr(mod, "_ENABLED", False)
    sent: list = []

    fake_messenger = type(sys)("agent_messenger")
    fake_messenger.send_message = lambda **kw: sent.append(kw)
    monkeypatch.setitem(sys.modules, "agent_messenger", fake_messenger)
    monkeypatch.setattr(
        mod,
        "_read_atom",
        lambda _id: {"id": "x", "text": "always test", "kind": "correction", "confidence": 0.95},
    )

    mod.on_memory_stored(mem_id="x")
    assert sent == []


def test_on_memory_stored_skip_operation_no_op(monkeypatch):
    mod = _load_hook()
    monkeypatch.setattr(mod, "_ENABLED", True)
    called = []
    monkeypatch.setattr(mod, "_read_atom", lambda _id: called.append(_id) or None)
    mod.on_memory_stored(mem_id="abc", operation="skip")
    assert called == [], "skip operation must short-circuit before reading the atom"


def test_on_memory_stored_correction_fans_out(monkeypatch):
    mod = _load_hook()
    monkeypatch.setattr(mod, "_ENABLED", True)
    monkeypatch.setattr(
        mod,
        "_read_atom",
        lambda _id: {
            "id": "x",
            "text": "no, it's gpt-5.5 not gpt-5.4",
            "kind": "correction",
            "confidence": 0.95,
        },
    )
    sent: list = []
    fake_messenger = type(sys)("agent_messenger")
    fake_messenger.send_message = lambda **kw: sent.append(kw)
    monkeypatch.setitem(sys.modules, "agent_messenger", fake_messenger)

    mod.on_memory_stored(mem_id="x", category="fact", operation="upsert")

    # Default broadcast hits at least claude + codex + jenna
    targets_sent = {kw.get("to_agent") for kw in sent}
    assert {"claude", "codex", "jenna"}.issubset(targets_sent)
    # Every send carries message_type=lesson + atom metadata
    for kw in sent:
        assert kw["message_type"] == "lesson"
        assert kw["from_agent"] == "brain"
        assert kw["metadata"]["atom_kind"] == "correction"


def test_on_memory_stored_atom_missing_no_op(monkeypatch):
    mod = _load_hook()
    monkeypatch.setattr(mod, "_ENABLED", True)
    monkeypatch.setattr(mod, "_read_atom", lambda _id: None)
    sent: list = []
    fake_messenger = type(sys)("agent_messenger")
    fake_messenger.send_message = lambda **kw: sent.append(kw)
    monkeypatch.setitem(sys.modules, "agent_messenger", fake_messenger)

    mod.on_memory_stored(mem_id="missing")
    assert sent == []
