"""Unit tests for brain_core/self_modify.py — the self-modification
proposer that writes routing changes to disk."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core"))


def test_atomic_write_parent_must_exist(tmp_path):
    """_atomic_write does NOT create parent dirs (caller's job)."""
    from self_modify import _atomic_write

    # Parent exists → succeeds
    target = tmp_path / "routes.json"
    assert _atomic_write(target, '{"a": 1}') is True
    assert target.exists()
    assert target.read_text() == '{"a": 1}'


def test_atomic_write_tempfile_rename_semantics(tmp_path):
    """Verify tempfile-then-rename pattern: result file is the target, not .tmp."""
    from self_modify import _atomic_write

    target = tmp_path / "x.json"
    assert _atomic_write(target, "content") is True
    # No .tmp leftover
    assert not target.with_suffix(".json.tmp").exists()
    assert target.read_text() == "content"


def test_apply_patch_add_intent():
    from self_modify import _apply_patch

    routes = {"intents": {"existing": {"keywords_en": []}}}
    patch = {
        "op": "add_intent",
        "intent": "new_intent",
        "keywords_en": ["foo", "bar"],
        "priority": "high",
    }
    ok, msg, new_routes = _apply_patch(routes, patch)
    assert ok is True
    assert "new_intent" in new_routes["intents"]
    assert new_routes["intents"]["new_intent"]["priority"] == "high"


def test_apply_patch_add_keyword():
    from self_modify import _apply_patch

    routes = {"intents": {"test_intent": {"keywords_en": ["existing"], "keywords_ko": []}}}
    patch = {"op": "add_keyword", "intent": "test_intent", "keywords_en": ["new_kw"]}
    ok, msg, new_routes = _apply_patch(routes, patch)
    assert ok is True
    assert "new_kw" in new_routes["intents"]["test_intent"]["keywords_en"]
    # Duplicate should not be re-added
    patch2 = {"op": "add_keyword", "intent": "test_intent", "keywords_en": ["new_kw"]}
    ok2, _, new_routes2 = _apply_patch(new_routes, patch2)
    assert ok2 is True
    assert new_routes2["intents"]["test_intent"]["keywords_en"].count("new_kw") == 1


def test_apply_patch_remove_intent():
    from self_modify import _apply_patch

    routes = {"intents": {"doomed": {"keywords_en": []}}}
    patch = {"op": "remove_intent", "intent": "doomed"}
    ok, msg, new_routes = _apply_patch(routes, patch)
    assert ok is True
    assert "doomed" not in new_routes["intents"]


def test_apply_patch_rejects_unknown_op():
    from self_modify import _apply_patch

    routes = {"intents": {}}
    patch = {"op": "nonsense", "intent": "whatever"}
    ok, msg, _ = _apply_patch(routes, patch)
    assert ok is False
    assert "unknown op" in msg


def test_apply_patch_rejects_missing_fields():
    from self_modify import _apply_patch

    routes = {"intents": {}}
    # Missing op
    ok, msg, _ = _apply_patch(routes, {"intent": "foo"})
    assert ok is False
    # Missing intent
    ok, msg, _ = _apply_patch(routes, {"op": "add_intent"})
    assert ok is False
