from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "brain_core"))

import skill_materializer  # noqa: E402
import skill_promotion_audit  # noqa: E402


def _write_proc_db(path: Path, *, success_count: int = 3) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "create table procedures (id text, task_type text, title text, steps text, preconditions text, tools text, success_count int, last_used text, created_at text, source text)"
    )
    conn.execute("create table outcomes (id text, task_id text, chris_override int, procedure_ids text)")
    conn.execute(
        "insert into procedures values (?,?,?,?,?,?,?,?,?,?)",
        (
            "proc_test",
            "test promotion",
            "Test promotion",
            json.dumps(["one", "two", "three"]),
            "",
            "[]",
            success_count,
            "2026-05-05T00:00:00+00:00",
            "2026-05-05T00:00:00+00:00",
            "extraction",
        ),
    )
    conn.commit()
    conn.close()


def _write_skill(root: Path, *, contract: bool = True) -> None:
    skill = root / "auto-test-promotion"
    skill.mkdir(parents=True)
    fm = (
        "---\n"
        "name: auto-test-promotion\n"
        "auto_generated: true\n"
        "brain_procedure_id: proc_test\n"
        "success_count: 3\n"
    )
    if contract:
        fm += (
            f"promotion_contract_version: {skill_materializer.PROMOTION_CONTRACT_VERSION}\n"
            "source_episode_count: 3\n"
            "rollback_strategy: archive_generated_auto_skill_dir\n"
        )
    (skill / "SKILL.md").write_text(fm + "---\n# skill\n")
    (root / skill_materializer.USAGE_FILE).write_text(
        json.dumps(
            {
                "auto-test-promotion": {
                    "brain_procedure_id": "proc_test",
                    "promotion_contract_version": skill_materializer.PROMOTION_CONTRACT_VERSION
                    if contract
                    else "",
                    "success_count": 3,
                }
            }
        )
    )


def test_skill_promotion_audit_ok_with_contract_all_runtimes(tmp_path):
    roots = (tmp_path / "claude", tmp_path / "codex", tmp_path / "openclaw")
    for root in roots:
        _write_skill(root, contract=True)
    db = tmp_path / "autonomy.db"
    _write_proc_db(db)

    out = skill_promotion_audit.skill_promotion_audit_snapshot(roots=roots, db_path=db)

    assert out["status"] == "ok"
    assert out["coverage"] == {"auto_skills": 1, "contract_ok": 1, "required_runtimes": 3}
    assert out["skills"][0]["usage_contract_count"] == 3
    assert out["outcome_delta"]["status"] == "ok"
    assert out["outcome_maturity"]["status"] == "insufficient_data"
    assert out["outcome_maturity"]["readiness_blocking"] is True


def test_skill_promotion_audit_links_procedure_outcomes(tmp_path):
    roots = (tmp_path / "claude", tmp_path / "codex", tmp_path / "openclaw")
    for root in roots:
        _write_skill(root, contract=True)
    db = tmp_path / "autonomy.db"
    _write_proc_db(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "insert into outcomes values (?,?,?,?)",
        ("outcome_1", "task_1", 0, json.dumps(["proc_test"])),
    )
    conn.commit()
    conn.close()

    out = skill_promotion_audit.skill_promotion_audit_snapshot(roots=roots, db_path=db)

    assert out["outcome_delta"]["linked_outcomes"] == 1
    assert out["outcome_delta"]["procedures_with_outcomes"] == 1
    assert out["outcome_delta"]["success_rate"] == 100.0
    assert out["outcome_maturity"]["status"] == "insufficient_data"


def test_skill_promotion_audit_blocks_missing_contract(tmp_path):
    roots = (tmp_path / "claude", tmp_path / "codex", tmp_path / "openclaw")
    for root in roots:
        _write_skill(root, contract=False)
    db = tmp_path / "autonomy.db"
    _write_proc_db(db)

    out = skill_promotion_audit.skill_promotion_audit_snapshot(roots=roots, db_path=db)

    assert out["status"] == "blocked"
    assert out["blockers"] == ["auto-test-promotion"]
    assert "missing_promotion_contract" in out["skills"][0]["reasons"]
    assert "missing_usage_contract" in out["skills"][0]["reasons"]


def test_skill_promotion_outcome_maturity_ok_after_minimum_outcomes(tmp_path):
    roots = (tmp_path / "claude", tmp_path / "codex", tmp_path / "openclaw")
    for root in roots:
        _write_skill(root, contract=True)
    db = tmp_path / "autonomy.db"
    _write_proc_db(db)
    with sqlite3.connect(db) as conn:
        for idx in range(5):
            conn.execute(
                "insert into outcomes values (?,?,?,?)",
                (f"outcome_{idx}", f"task_{idx}", 0, json.dumps(["proc_test"])),
            )

    out = skill_promotion_audit.skill_promotion_audit_snapshot(roots=roots, db_path=db)

    assert out["outcome_maturity"]["status"] == "ok"
    assert out["outcome_maturity"]["readiness_blocking"] is False
    assert out["outcome_maturity"]["task_linked_outcomes"] == 5
    assert out["outcome_maturity"]["source_success_count"] == 3
    assert out["outcome_maturity"]["linked_outcomes"] == 8
