"""Unit tests for brain_core.eval_proposals (Phase 7 closed-loop pipeline)."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


@pytest.fixture
def isolated_proposals(tmp_path, monkeypatch):
    """Point eval_proposals at a fresh tmp_path autonomy.db."""
    for mod in ("eval_proposals", "config"):
        if mod in sys.modules:
            del sys.modules[mod]
    import eval_proposals

    fake_db = tmp_path / "autonomy.db"
    monkeypatch.setattr(eval_proposals, "AUTONOMY_DB", fake_db)
    monkeypatch.setattr(eval_proposals, "_initialized", False)
    yield eval_proposals
    importlib.reload(eval_proposals)


def test_insert_then_list(isolated_proposals):
    pid = isolated_proposals.insert_proposal(
        query="what is autonomy?",
        expected="L0-L3 gate",
        source_event="recall_feedback",
    )
    assert pid is not None
    assert pid.startswith("prop_")
    rows = isolated_proposals.list_candidates()
    assert len(rows) == 1
    assert rows[0]["id"] == pid
    assert rows[0]["query"] == "what is autonomy?"
    assert rows[0]["status"] == "candidate"


def test_insert_with_expected_sources_serializes_json(isolated_proposals):
    import json

    pid = isolated_proposals.insert_proposal(
        query="q",
        expected="e",
        expected_sources=["canonical:a.md", "obsidian:b.md"],
    )
    rows = isolated_proposals.list_candidates()
    payload = json.loads(rows[0]["expected_sources"])
    assert payload == ["canonical:a.md", "obsidian:b.md"]
    assert pid is not None


def test_insert_rejects_empty_query_or_expected(isolated_proposals):
    assert isolated_proposals.insert_proposal(query="", expected="e") is None
    assert isolated_proposals.insert_proposal(query="q", expected="") is None


def test_mark_status_pending_clears_promoted_at(isolated_proposals):
    pid = isolated_proposals.insert_proposal(query="q", expected="e")
    assert isolated_proposals.mark_status(pid, "pending", novelty_score=0.7) is True
    rows = isolated_proposals.list_candidates(status="pending")
    assert len(rows) == 1
    assert rows[0]["novelty_score"] == 0.7
    # promoted_at only set when status='promoted'
    assert rows[0]["promoted_at"] is None


def test_mark_status_promoted_records_promoted_at(isolated_proposals):
    pid = isolated_proposals.insert_proposal(query="q", expected="e")
    isolated_proposals.mark_status(pid, "promoted", novelty_score=0.85)
    rows = isolated_proposals.list_candidates(status="promoted")
    assert len(rows) == 1
    assert rows[0]["promoted_at"] is not None
    assert rows[0]["novelty_score"] == 0.85


def test_mark_status_rejected_records_reviewed_at(isolated_proposals):
    pid = isolated_proposals.insert_proposal(query="q", expected="e")
    isolated_proposals.mark_status(pid, "rejected", novelty_score=0.1)
    rows = isolated_proposals.list_candidates(status="rejected")
    assert len(rows) == 1
    assert rows[0]["reviewed_at"] is not None


def test_mark_status_invalid_raises(isolated_proposals):
    pid = isolated_proposals.insert_proposal(query="q", expected="e")
    with pytest.raises(ValueError, match="invalid status"):
        isolated_proposals.mark_status(pid, "deleted")


def test_stats_aggregates_by_status(isolated_proposals):
    p1 = isolated_proposals.insert_proposal(query="q1", expected="e1")
    p2 = isolated_proposals.insert_proposal(query="q2", expected="e2")
    p3 = isolated_proposals.insert_proposal(query="q3", expected="e3")
    isolated_proposals.mark_status(p1, "promoted", novelty_score=0.9)
    isolated_proposals.mark_status(p2, "rejected", novelty_score=0.1)
    # p3 stays candidate
    counts = isolated_proposals.stats()
    assert counts.get("candidate") == 1
    assert counts.get("promoted") == 1
    assert counts.get("rejected") == 1
    assert p3 is not None
