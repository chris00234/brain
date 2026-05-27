#!/usr/bin/env python3
"""Audit required Brain/Hermes config and secret presence without printing values."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BRAIN_ROOT = Path(__file__).resolve().parents[1]
REPORT_FILE = BRAIN_ROOT / "logs" / "config_secret_audit.json"
BRAIN_CREDENTIALS_DIR = Path.home() / ".brain" / "credentials"
HERMES_DIR = Path.home() / ".hermes"

REQUIRED_FILES = {
    "brain_bearer_secret": BRAIN_CREDENTIALS_DIR / ".personal_webhook_secret",
    "hermes_config": HERMES_DIR / "config.yaml",
    "hermes_jenna_cron_jobs": HERMES_DIR / "profiles" / "jenna" / "cron" / "jobs.json",
    "minio_env": Path("/Users/chrischo/server/minio/.env"),
}
REQUIRED_ENV_OR_FILE_HINTS = {
    "telegram_token": (
        "TELEGRAM_JENNA_TOKEN|TELEGRAM_BOT_TOKEN",
        (HERMES_DIR / ".env", HERMES_DIR / "profiles" / "jenna" / ".env"),
    ),
}


def _file_status(path: Path) -> dict[str, Any]:
    exists = path.exists()
    return {
        "path": str(path),
        "exists": exists,
        "bytes": path.stat().st_size if exists else 0,
        "readable": os.access(path, os.R_OK) if exists else False,
    }


def _file_contains_any(path: Path, keys: tuple[str, ...]) -> bool:
    if not path.exists():
        return False
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return False
    return any(f"{key}=" in text or f"export {key}=" in text for key in keys)


def _paths_contains_any(paths: Path | tuple[Path, ...], keys: tuple[str, ...]) -> bool:
    if isinstance(paths, Path):
        return _file_contains_any(paths, keys)
    return any(_file_contains_any(path, keys) for path in paths)


def _launchd_env(service: str = "ai.brain.server") -> dict[str, bool]:
    try:
        out = subprocess.check_output(
            ["launchctl", "print", f"gui/{os.getuid()}/{service}"],
            text=True,
            timeout=5,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return {"loaded": False}
    return {
        "loaded": True,
        "has_qdrant_url": "QDRANT_URL" in out,
        "has_vector_backend": "VECTOR_BACKEND" in out,
        "has_brain_embed_model": "BRAIN_EMBED_MODEL" in out,
    }


def run() -> dict[str, Any]:
    files = {name: _file_status(path) for name, path in REQUIRED_FILES.items()}
    env_or_file = {}
    for name, (env_expr, path) in REQUIRED_ENV_OR_FILE_HINTS.items():
        keys = tuple(env_expr.split("|"))
        env_or_file[name] = {
            "env_names": list(keys),
            "present_in_process_env": any(bool(os.getenv(k)) for k in keys),
            "present_in_file": _paths_contains_any(path, keys),
            "file": str(path) if isinstance(path, Path) else [str(p) for p in path],
        }
    issues = []
    for name, status in files.items():
        if not status["exists"] or not status["readable"] or status["bytes"] <= 0:
            issues.append({"kind": "file", "name": name, "issue": "missing_or_unreadable"})
    for name, status in env_or_file.items():
        if not status["present_in_process_env"] and not status["present_in_file"]:
            issues.append({"kind": "secret_source", "name": name, "issue": "missing_env_or_export_file"})
    launchd = _launchd_env()
    if not launchd.get("loaded"):
        issues.append({"kind": "launchd", "name": "brain-server", "issue": "service_not_loaded"})
    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "status": "ok" if not issues else "warning",
        "files": files,
        "env_or_file": env_or_file,
        "launchd": launchd,
        "issues": issues,
    }
    REPORT_FILE.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return report


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    raise SystemExit(0 if result["status"] == "ok" else 1)
