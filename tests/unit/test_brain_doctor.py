from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("brain_doctor", ROOT / "cli" / "brain_doctor.py")
brain_doctor = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(brain_doctor)


def test_cli_llm_surface_reports_codex_without_claude_prompt_mode():
    surface = brain_doctor._cli_llm_surface()

    assert surface["status"] == "ok"
    assert surface["primary_backend"] == "codex"
    assert surface["primary_model"] == "gpt-5.5"
    assert surface["claude_backend_present"] is False
    assert surface["claude_prompt_mode_removed"] is True


def test_recall_structural_judgments_snapshot_reports_sidecar_and_legacy_counts(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE action_audit ("
            "id INTEGER PRIMARY KEY, outcome TEXT, route TEXT, query_text TEXT, created_at TEXT)"
        )
        conn.execute("INSERT INTO action_audit(outcome) VALUES ('structural_good')")
        conn.execute(
            "CREATE TABLE recall_structural_judgments ("
            "action_audit_id INTEGER PRIMARY KEY, outcome TEXT, structural_score REAL, "
            "reason_json TEXT, created_at TEXT, judged_at TEXT)"
        )
        conn.execute(
            "INSERT INTO recall_structural_judgments "
            "(action_audit_id, outcome, structural_score, reason_json, created_at, judged_at) "
            "VALUES (1, 'structural_wrong', 0.2, '{}', '2026-05-14T00:00:00Z', '2026-05-14T00:01:00Z')"
        )

    monkeypatch.setattr(brain_doctor, "BRAIN_DB", db)

    snapshot = brain_doctor._recall_structural_judgments_snapshot()

    assert snapshot["status"] == "ok"
    assert snapshot["table_exists"] is True
    assert snapshot["total"] == 1
    assert snapshot["outcome_counts"] == {"structural_wrong": 1}
    assert snapshot["legacy_action_outcome_counts"] == {"structural_good": 1}
