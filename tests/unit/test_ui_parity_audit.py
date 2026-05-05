from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("ui_parity_audit", ROOT / "cli" / "ui_parity_audit.py")
ui_parity_audit = importlib.util.module_from_spec(SPEC)
sys.modules["ui_parity_audit"] = ui_parity_audit
assert SPEC.loader is not None
SPEC.loader.exec_module(ui_parity_audit)


def test_ui_parity_audit_current_repo_is_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_parity_audit, "REPORT_FILE", tmp_path / "ui_parity_audit.json")

    out = ui_parity_audit.run()

    assert out["status"] == "ok"
    assert out["blocked"] == 0
    assert out["required"] >= 9
    assert out["coverage_level"] == "route_api_client_manifest_v1"
    assert out["readiness_manifest_version"] == "readiness-surface-manifest-v1"
    assert out["readiness_manifest"]["surface_count"] >= 7
    assert out["backend_route_count"] > 0
    assert out["api_client_path_count"] > 0
    assert (tmp_path / "ui_parity_audit.json").exists()


def test_ui_parity_audit_blocks_missing_ui_token(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_parity_audit, "REPORT_FILE", tmp_path / "ui_parity_audit.json")
    monkeypatch.setattr(
        ui_parity_audit,
        "CHECKS",
        (
            ui_parity_audit.ParityCheck(
                id="missing_ui",
                label="missing ui token",
                backend_tokens=("readiness_snapshot",),
                ui_tokens=("definitely_missing_ui_parity_token",),
            ),
        ),
    )

    out = ui_parity_audit.run()

    assert out["status"] == "blocked"
    assert out["blocked"] == 1
    assert out["rows"][0]["missing_ui_tokens"] == ["definitely_missing_ui_parity_token"]


def test_ui_parity_audit_blocks_missing_backend_route(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_parity_audit, "REPORT_FILE", tmp_path / "ui_parity_audit.json")
    monkeypatch.setattr(
        ui_parity_audit,
        "CHECKS",
        (
            ui_parity_audit.ParityCheck(
                id="missing_route",
                label="missing route",
                backend_paths=("/brain/definitely-missing-route",),
            ),
        ),
    )

    out = ui_parity_audit.run()

    assert out["status"] == "blocked"
    assert out["rows"][0]["missing_backend_paths"] == ["/brain/definitely-missing-route"]


def test_ui_parity_audit_blocks_missing_api_client_path(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_parity_audit, "REPORT_FILE", tmp_path / "ui_parity_audit.json")
    monkeypatch.setattr(
        ui_parity_audit,
        "CHECKS",
        (
            ui_parity_audit.ParityCheck(
                id="missing_api_client",
                label="missing api client",
                api_client_paths=("/brain/definitely-missing-client-path",),
            ),
        ),
    )

    out = ui_parity_audit.run()

    assert out["status"] == "blocked"
    assert out["rows"][0]["missing_api_client_paths"] == ["/brain/definitely-missing-client-path"]
