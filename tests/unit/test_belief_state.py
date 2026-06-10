from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "brain_core"))

from belief_state import build_belief_state  # noqa: E402
from decision_ledger import record_decision  # noqa: E402
from task_queue import TaskQueue  # noqa: E402


def _iso(days_ago: int = 0) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat(timespec="seconds").replace("+00:00", "Z")


def _init_atoms(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE atoms (
                id TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'fact',
                tier TEXT NOT NULL DEFAULT 'episodic',
                canonical INTEGER NOT NULL DEFAULT 0,
                confidence REAL NOT NULL DEFAULT 0.5,
                trust_score REAL NOT NULL DEFAULT 0.5,
                quality_score REAL,
                valid_until TEXT,
                provenance_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL,
                provisional INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()


def _insert_atom(
    path: Path,
    *,
    atom_id: str,
    text: str,
    confidence: float,
    trust_score: float = 0.8,
    canonical: int = 0,
    tier: str = "semantic",
    kind: str = "preference",
    updated_at: str | None = None,
) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO atoms (
                id, text, kind, tier, canonical, confidence, trust_score,
                quality_score, valid_until, provenance_json, updated_at, provisional
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 0.7, NULL, '{}', ?, 0)
            """,
            (atom_id, text, kind, tier, canonical, confidence, trust_score, updated_at or _iso()),
        )
        conn.commit()


def test_belief_state_compiles_existing_signals_without_llm(tmp_path, monkeypatch):
    # _load_trend_alerts reads the production autonomy.db via brain_config_store;
    # a live drift alert (priority 0.85) can outrank the expected first action.
    monkeypatch.setattr("belief_state._load_trend_alerts", lambda warnings: [])
    brain_db = tmp_path / "brain.db"
    autonomy_db = tmp_path / "autonomy.db"
    _init_atoms(brain_db)
    _insert_atom(
        brain_db,
        atom_id="belief-high",
        text="Use subscription CLIs for LLM generation.",
        confidence=0.92,
        canonical=1,
    )
    _insert_atom(
        brain_db,
        atom_id="belief-low",
        text="Unverified preference candidate.",
        confidence=0.2,
    )

    tq = TaskQueue(autonomy_db)
    goal = tq.create_goal(
        "Improve brain quality",
        "Reduce regressions without hot-path LLM calls.",
        metadata={"priority": 9},
    )
    task = tq.create_task(
        "Add belief state",
        parent_goal_id=goal["id"],
        confidence=0.7,
        metadata={"domain": "brain"},
    )
    tq.record_outcome(
        task["id"],
        domain="brain",
        brain_recommendation="Add deterministic state.",
        actual_action="Chris asked for edge-case review.",
        chris_override=True,
        override_reason="needs better abstraction",
    )
    record_decision(
        domain="brain",
        source="brain_loop",
        observation_kind="task_dispatch",
        observation_subject=task["id"],
        selected_option="dispatch_agent",
        confidence=0.88,
        actual_outcome="dispatch failed and needs review",
        outcome_status="failed",
        review_status="needs_review",
        db_path=autonomy_db,
    )

    state = build_belief_state(brain_db=brain_db, task_queue_obj=tq, limit=10)

    assert state["policy"]["llm"] == "none"
    assert state["policy"]["mode"] == "deterministic_read_only"
    assert state["summary"]["beliefs"] == 1
    assert state["summary"]["uncertainties"] == 1
    assert state["summary"]["decision_feedback_candidates"] == 1
    assert state["world_model"]["agency_level"] == "review_first_closed_loop"
    assert state["world_model"]["highest_risk"]["type"] == "decision_feedback"
    assert state["operating_constraints"][0]["id"] == "no_extra_llm_api_cost"
    assert state["beliefs"][0]["id"] == "belief-high"
    assert state["uncertainties"][0]["id"] == "belief-low"
    assert state["uncertainties"][0]["needs_review"] is True
    assert state["goals"][0]["id"] == goal["id"]
    assert "metadata_priority" in state["goals"][0]["priority_reasons"]
    assert state["recent_outcomes"][0]["chris_override"] is True
    assert state["next_actions"][0]["type"] == "review_decision_feedback"


def test_belief_state_marks_stale_canonical_as_uncertainty(tmp_path):
    brain_db = tmp_path / "brain.db"
    _init_atoms(brain_db)
    _insert_atom(
        brain_db,
        atom_id="stale-canonical",
        text="Old canonical belief.",
        confidence=0.9,
        canonical=1,
        updated_at=_iso(days_ago=365),
    )

    state = build_belief_state(brain_db=brain_db, task_queue_obj=TaskQueue(tmp_path / "a.db"))

    assert state["uncertainties"][0]["id"] == "stale-canonical"
    assert state["uncertainties"][0]["reason"] == "stale_canonical"
    assert state["uncertainties"][0]["freshness"] == "stale"


def test_belief_state_excludes_dream_conjectures_from_uncertainties(tmp_path):
    """dream_replay emits kind='conjecture' atoms at confidence=0.3 by design.
    Surfacing them as uncertainties drowns real low-confidence beliefs and
    stale canonicals (the things actually worth reviewing)."""
    brain_db = tmp_path / "brain.db"
    _init_atoms(brain_db)
    _insert_atom(
        brain_db,
        atom_id="dream-conjecture-1",
        text="Dream conjecture (foo x bar): hypothetical link.",
        confidence=0.3,
        kind="conjecture",
        tier="episodic",
    )
    _insert_atom(
        brain_db,
        atom_id="real-low-confidence",
        text="Actual unverified preference.",
        confidence=0.25,
        kind="preference",
    )

    state = build_belief_state(brain_db=brain_db, task_queue_obj=TaskQueue(tmp_path / "a.db"))

    uncertainty_ids = [u["id"] for u in state["uncertainties"]]
    assert "dream-conjecture-1" not in uncertainty_ids
    assert "real-low-confidence" in uncertainty_ids


def test_belief_state_fails_soft_when_atoms_db_missing(tmp_path, monkeypatch):
    # _load_trend_alerts reads the production autonomy.db via brain_config_store,
    # leaking live metric-drift state into this hermetic test.
    monkeypatch.setattr("belief_state._load_trend_alerts", lambda warnings: [])
    state = build_belief_state(
        brain_db=tmp_path / "missing.db",
        task_queue_obj=TaskQueue(tmp_path / "autonomy.db"),
    )

    assert state["beliefs"] == []
    assert state["uncertainties"] == []
    assert state["warnings"][0]["source"] == "atoms"
    assert state["next_actions"][0]["type"] == "observe"
