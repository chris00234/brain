from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("config_secret_audit", ROOT / "cli" / "config_secret_audit.py")
config_secret_audit = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(config_secret_audit)


def test_config_secret_audit_reports_presence_without_values(tmp_path, monkeypatch):
    secret = tmp_path / "secret"
    secret.write_text("super-secret-value")
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_JENNA_TOKEN=hidden")
    monkeypatch.setattr(config_secret_audit, "REQUIRED_FILES", {"secret": secret})
    monkeypatch.setattr(
        config_secret_audit, "REQUIRED_ENV_OR_FILE_HINTS", {"telegram": ("TELEGRAM_JENNA_TOKEN", env_file)}
    )
    monkeypatch.setattr(config_secret_audit, "REPORT_FILE", tmp_path / "report.json")
    monkeypatch.setattr(config_secret_audit, "_launchd_env", lambda: {"loaded": True})

    out = config_secret_audit.run()

    assert out["status"] == "ok"
    dumped = str(out)
    assert "super-secret-value" not in dumped
    assert "hidden" not in dumped
