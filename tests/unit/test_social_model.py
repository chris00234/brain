from __future__ import annotations

import sys
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


def _reload(tmp_path, monkeypatch):
    for mod in [m for m in list(sys.modules) if m in ("social_model", "db")]:
        del sys.modules[mod]
    import db
    import social_model as sm

    monkeypatch.setattr(sm, "DB_PATH", tmp_path / "autonomy.db")
    db._schema_cache.clear()
    return sm


def test_record_and_get_belief(tmp_path, monkeypatch):
    sm = _reload(tmp_path, monkeypatch)
    r = sm.record_belief("jenna", "Has root access to brain.db", source="test", confidence=0.7)
    assert r["ok"] is True
    model = sm.get_subject_model("jenna")
    assert model["count"] == 1
    assert model["beliefs"][0]["belief"] == "Has root access to brain.db"
    assert model["beliefs"][0]["confidence"] == 0.7


def test_supersede_chain(tmp_path, monkeypatch):
    sm = _reload(tmp_path, monkeypatch)
    first = sm.record_belief("liz", "Believes brain is unstable", confidence=0.6)
    sm.record_belief(
        "liz",
        "Believes brain is stable after 2026-05 fixes",
        confidence=0.85,
        supersedes=first["id"],
    )
    model = sm.get_subject_model("liz")
    assert model["count"] == 1
    assert "stable" in model["beliefs"][0]["belief"]


def test_seed_idempotent(tmp_path, monkeypatch):
    sm = _reload(tmp_path, monkeypatch)
    first = sm.seed_known_agents()
    second = sm.seed_known_agents()
    assert first["inserted"] == len(sm.SEEDS)
    assert second["inserted"] == 0
    assert second["skipped"] == len(sm.SEEDS)
    subjects = sm.list_subjects()
    assert any(s["subject"] == "jenna" for s in subjects)
    assert any(s["subject"] == "chris" for s in subjects)
