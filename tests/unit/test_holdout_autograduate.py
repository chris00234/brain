"""Phase N3 — holdout auto-graduation + lifecycle tracker."""

from __future__ import annotations

import importlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


@pytest.fixture
def holdout_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_ATOMS_ENABLED", "true")
    for mod in ("atoms_store", "config", "eval_holdout_promote"):
        if mod in sys.modules:
            del sys.modules[mod]
    import atoms_store

    fake_db = tmp_path / "brain.db"
    monkeypatch.setattr(atoms_store, "BRAIN_ATOMS_ENABLED", True)
    monkeypatch.setattr(atoms_store, "BRAIN_DB", fake_db)
    monkeypatch.setattr(atoms_store, "_initialized", False)
    atoms_store.init_schema(fake_db)

    import eval_holdout_promote as ehp

    monkeypatch.setattr(ehp, "BRAIN_DB", fake_db)

    fake_eval_set = tmp_path / "eval_set.json"
    fake_pending = tmp_path / "eval_holdout_pending.json"
    monkeypatch.setattr(ehp, "EVAL_SET_PATH", fake_eval_set)
    monkeypatch.setattr(ehp, "PENDING_PATH", fake_pending)

    fake_eval_set.write_text(
        json.dumps(
            [{"query": "baseline", "expected_source": "", "expected_content": "baseline"}],
            indent=2,
        )
    )

    candidates = []
    now_iso = datetime.now(UTC).isoformat(timespec="seconds")
    for i in range(10):
        candidates.append(
            {
                "id": f"cand_{i}",
                "query": f"test query {i}",
                "expected": f"expected answer {i}",
                "expected_sources": [],
                "novelty": 0.5,
                "promoted_at": now_iso,
            }
        )
    fake_pending.write_text(json.dumps(candidates, indent=2))

    for cand in candidates:
        conn = ehp._lifecycle_conn()
        try:
            ehp._ensure_lifecycle_row(conn, cand["id"], cand["promoted_at"])
            conn.commit()
        finally:
            conn.close()

    yield ehp, fake_db, fake_eval_set, fake_pending, candidates

    importlib.reload(atoms_store)


def test_auto_graduate_moves_passing_candidates(holdout_env):
    ehp, _fake_db, fake_eval_set, fake_pending, _ = holdout_env

    for i in range(6):
        for _ in range(4):
            ehp.record_eval_result(f"cand_{i}", True)
    for i in range(8, 10):
        for _ in range(5):
            ehp.record_eval_result(f"cand_{i}", False)

    result = ehp.auto_graduate()

    assert result["graduated"] == 5, f"expected 5 graduated, got {result['graduated']}"
    assert result["rejected"] == 2, f"expected 2 rejected, got {result['rejected']}"
    assert result["cap_reached"] is True

    data = json.loads(fake_eval_set.read_text())
    assert len(data) == 6, f"eval_set should be baseline + 5 grads, got {len(data)}"
    assert any(e.get("_graduated_from_holdout") for e in data)

    remaining = json.loads(fake_pending.read_text())
    assert len(remaining) == 3, f"expected 3 pending (10 - 7), got {len(remaining)}"

    backup = fake_eval_set.with_suffix(fake_eval_set.suffix + ".backup")
    assert backup.exists(), "backup should be written before rewrite"


def test_record_eval_result_increments_runs_and_passes(holdout_env):
    ehp, _, _, _, _ = holdout_env
    r1 = ehp.record_eval_result("cand_0", True)
    assert r1["eval_runs"] == 1
    assert r1["eval_passes"] == 1
    r2 = ehp.record_eval_result("cand_0", False)
    assert r2["eval_runs"] == 2
    assert r2["eval_passes"] == 1


def test_stuck_candidates_ignores_fresh_pending(holdout_env):
    ehp, _, _, _, _ = holdout_env
    stuck = ehp.stuck_candidates()
    assert stuck == [], "freshly-promoted candidates must not appear stuck"


def test_stuck_candidates_detects_old_pending(holdout_env):
    ehp, fake_db, _, _, _ = holdout_env
    import sqlite3

    old = (datetime.now(UTC) - timedelta(days=30)).isoformat(timespec="seconds")
    conn = sqlite3.connect(str(fake_db))
    try:
        conn.execute(
            "UPDATE eval_holdout_lifecycle SET promoted_at = ? WHERE candidate_id = ?",
            (old, "cand_0"),
        )
        conn.commit()
    finally:
        conn.close()
    stuck = ehp.stuck_candidates()
    stuck_ids = {row["candidate_id"] for row in stuck}
    assert "cand_0" in stuck_ids
    assert len(stuck) == 1


def test_auto_graduate_skips_when_below_threshold(holdout_env):
    ehp, _, fake_eval_set, _, _ = holdout_env
    for _ in range(3):
        ehp.record_eval_result("cand_0", True)
    result = ehp.auto_graduate()
    assert result["graduated"] == 0
    assert result["rejected"] == 0
    data = json.loads(fake_eval_set.read_text())
    assert len(data) == 1
