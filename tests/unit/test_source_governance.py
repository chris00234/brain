from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "brain_core"))

import source_governance  # noqa: E402


def test_openclaw_source_freshness_matches_intentional_daytime_schedule_gap():
    openclaw = next(
        source for source in source_governance.GOVERNED_SOURCES if source.id == "openclaw_sessions"
    )

    assert openclaw.max_age_hours >= 14


def test_source_governance_ok_when_critical_source_is_fresh(tmp_path, monkeypatch):
    now = datetime(2026, 5, 5, 5, 0, tzinfo=UTC)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "personal-state.json").write_text(
        json.dumps({"last_run": (now - timedelta(hours=1)).isoformat()})
    )
    monkeypatch.setattr(source_governance, "LOGS_DIR", logs)
    monkeypatch.setattr(
        source_governance,
        "GOVERNED_SOURCES",
        (
            source_governance.GovernedSource(
                id="personal",
                label="Personal",
                jobs=("personal_ingest",),
                state_files=("personal-state.json",),
                max_age_hours=12,
            ),
        ),
    )
    monkeypatch.setattr(source_governance, "CONTROL_CHECKS", ())
    monkeypatch.setattr(source_governance, "_load_job_registry", lambda: {"personal_ingest"})
    monkeypatch.setattr(source_governance, "_load_job_schedule", lambda: {"personal_ingest"})
    monkeypatch.setattr(source_governance, "_latest_job_record", lambda jobs, db_path=Path(): None)

    out = source_governance.source_governance_snapshot(now=now)

    assert out["status"] == "ok"
    assert out["coverage"]["critical_sources_ok"] == 1
    assert out["sources"][0]["age_hours"] == 1.0


def test_source_governance_blocks_stale_critical_source(tmp_path, monkeypatch):
    now = datetime(2026, 5, 5, 5, 0, tzinfo=UTC)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "personal-state.json").write_text(
        json.dumps({"last_run": (now - timedelta(hours=13)).isoformat()})
    )
    monkeypatch.setattr(source_governance, "LOGS_DIR", logs)
    monkeypatch.setattr(
        source_governance,
        "GOVERNED_SOURCES",
        (
            source_governance.GovernedSource(
                id="personal",
                label="Personal",
                jobs=("personal_ingest",),
                state_files=("personal-state.json",),
                max_age_hours=12,
            ),
        ),
    )
    monkeypatch.setattr(source_governance, "CONTROL_CHECKS", ())
    monkeypatch.setattr(source_governance, "_load_job_registry", lambda: {"personal_ingest"})
    monkeypatch.setattr(source_governance, "_load_job_schedule", lambda: {"personal_ingest"})
    monkeypatch.setattr(source_governance, "_latest_job_record", lambda jobs, db_path=Path(): None)

    out = source_governance.source_governance_snapshot(now=now)

    assert out["status"] == "blocked"
    assert out["blockers"] == ["personal"]
    assert out["sources"][0]["reasons"] == ["stale"]


def test_source_governance_warns_on_nonrequired_provenance_lint_findings(tmp_path, monkeypatch):
    now = datetime(2026, 5, 5, 5, 0, tzinfo=UTC)
    root = tmp_path / "brain"
    logs = root / "logs"
    logs.mkdir(parents=True)
    (logs / "memory-provenance-lint.json").write_text(
        json.dumps({"timestamp": (now - timedelta(hours=1)).isoformat(), "errors": 2})
    )
    monkeypatch.setattr(source_governance, "BRAIN_ROOT", root)
    monkeypatch.setattr(source_governance, "LOGS_DIR", logs)
    monkeypatch.setattr(source_governance, "GOVERNED_SOURCES", ())
    monkeypatch.setattr(
        source_governance,
        "CONTROL_CHECKS",
        (
            {
                "id": "memory_provenance_lint",
                "label": "lint",
                "logs": ("memory-provenance-lint.json",),
                "max_age_hours": 48,
                "json_zero_fields": ("errors",),
                "required": False,
            },
        ),
    )
    monkeypatch.setattr(source_governance, "_load_job_registry", lambda: set())
    monkeypatch.setattr(source_governance, "_load_job_schedule", lambda: set())

    out = source_governance.source_governance_snapshot(now=now)

    assert out["status"] == "warning"
    assert out["blockers"] == []
    assert out["warnings"] == ["memory_provenance_lint"]
    assert out["controls"][0]["json_findings"] == {"errors": 2}
