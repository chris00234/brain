from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "cli" / "migrate_structural_judgments_sidecar.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("migrate_structural_judgments_sidecar", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _create_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE action_audit (
                id INTEGER PRIMARY KEY,
                outcome TEXT,
                outcome_reason TEXT,
                created_at TEXT,
                resolved_at TEXT,
                route TEXT,
                actor TEXT,
                tool TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO action_audit
              (id, outcome, outcome_reason, created_at, resolved_at, route, actor, tool)
            VALUES
              (1, 'structural_good', 'heuristic ok', '2026-05-01T00:00:00Z', '2026-05-01T00:01:00Z', '/brain/recall', 'brain', 'recall_judge'),
              (2, 'judged_good', 'human ok', '2026-05-01T00:02:00Z', '2026-05-01T00:03:00Z', '/brain/recall', 'chris', 'manual'),
              (3, NULL, NULL, '2026-05-01T00:04:00Z', NULL, '/brain/other', 'brain', 'noop')
            """
        )


def test_structural_migration_dry_run_does_not_mutate(tmp_path: Path):
    mod = _load_module()
    db = tmp_path / "brain.db"
    _create_db(db)

    result = mod.migrate(db, apply=False)

    assert result["ok"] is True
    assert result["applied"] is False
    assert result["legacy_count"] == 1
    assert result["legacy_counts"] == {"structural_good": 1}
    with sqlite3.connect(db) as conn:
        outcome = conn.execute("SELECT outcome FROM action_audit WHERE id = 1").fetchone()[0]
        sidecar = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='recall_structural_judgments'"
        ).fetchone()
    assert outcome == "structural_good"
    assert sidecar is None


def test_structural_migration_apply_backs_up_sidecar_and_clears_legacy(tmp_path: Path):
    mod = _load_module()
    db = tmp_path / "brain.db"
    backup = tmp_path / "brain.db.bak"
    _create_db(db)

    result = mod.migrate(db, apply=True, backup_path=backup)

    assert result["ok"] is True
    assert result["applied"] is True
    assert result["inserted_sidecar"] == 1
    assert result["cleared_action_audit"] == 1
    assert result["remaining_legacy_count"] == 0
    assert backup.exists()

    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        action = conn.execute(
            "SELECT outcome, outcome_reason, resolved_at FROM action_audit WHERE id = 1"
        ).fetchone()
        nonstructural = conn.execute(
            "SELECT outcome, outcome_reason FROM action_audit WHERE id = 2"
        ).fetchone()
        sidecar = conn.execute(
            "SELECT action_audit_id, outcome, structural_score, reason_json, created_at FROM recall_structural_judgments"
        ).fetchone()
    assert dict(action) == {"outcome": None, "outcome_reason": None, "resolved_at": None}
    assert dict(nonstructural) == {"outcome": "judged_good", "outcome_reason": "human ok"}
    assert sidecar["action_audit_id"] == 1
    assert sidecar["outcome"] == "structural_good"
    assert sidecar["structural_score"] == 1.0
    assert sidecar["created_at"] == "2026-05-01T00:00:00Z"
    reason = json.loads(sidecar["reason_json"])
    assert reason["source"] == "legacy_action_audit_outcome_backfill"
    assert reason["legacy_outcome_reason"] == "heuristic ok"

    with sqlite3.connect(backup) as conn:
        backup_outcome = conn.execute("SELECT outcome FROM action_audit WHERE id = 1").fetchone()[0]
    assert backup_outcome == "structural_good"

    again = mod.migrate(db, apply=True, backup_path=None)
    assert again["legacy_count"] == 0
    with sqlite3.connect(db) as conn:
        total = conn.execute("SELECT COUNT(*) FROM recall_structural_judgments").fetchone()[0]
    assert total == 1
