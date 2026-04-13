"""Global pytest fixtures for the brain test suite.

Layout:
    tests/unit/         — pure unit tests, no IO outside tmp_path
    tests/integration/  — needs Chroma/Ollama (marked @pytest.mark.integration)
    tests/e2e/          — spawns brain-server (marked @pytest.mark.e2e)
    tests/smoke/        — operator load/SLO scripts (excluded from collection)

Convention: every fixture that touches disk uses tmp_path; nothing writes to
~/server/brain/logs/ or ~/server/knowledge/ during a test run.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

BRAIN_ROOT = Path(__file__).resolve().parents[1]
BRAIN_CORE = BRAIN_ROOT / "brain_core"
PIPELINE = BRAIN_ROOT / "pipeline"
CLI = BRAIN_ROOT / "cli"

# brain_core uses ad-hoc sys.path insertion. Tests need the same.
for p in (BRAIN_CORE, PIPELINE, CLI, BRAIN_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


@pytest.fixture
def brain_env(tmp_path, monkeypatch):
    """Sandbox env vars + log dirs to tmp_path so tests never touch real brain state."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setenv("BRAIN_LOG_DIR", str(log_dir))
    monkeypatch.setenv("BRAIN_DISABLE_ATOMS", "1")
    monkeypatch.setenv("BRAIN_AUTOPILOT_DISABLED", "1")
    yield log_dir


@pytest.fixture
def temp_sqlite(tmp_path):
    """A throwaway SQLite path for tests that need their own DB."""
    db_path = tmp_path / "test.db"
    yield db_path
    if db_path.exists():
        db_path.unlink()


@pytest.fixture
def isolated_schema_versions(tmp_path, monkeypatch):
    """Point schema_versions at an isolated DB so we can apply real migrations without
    touching the production schema_versions.db."""
    import importlib

    monkeypatch.setenv("BRAIN_LOG_DIR", str(tmp_path))
    fake_db = tmp_path / "schema_versions.db"
    # Reload the module so VERSIONS_DB binding picks up via patching
    import schema_versions  # type: ignore

    monkeypatch.setattr(schema_versions, "VERSIONS_DB", fake_db)
    yield schema_versions
    importlib.reload(schema_versions)


def pytest_collection_modifyitems(config, items):
    """Auto-skip integration tests if BRAIN_INTEGRATION_TESTS is not set."""
    if os.environ.get("BRAIN_INTEGRATION_TESTS") == "1":
        return
    skip_integration = pytest.mark.skip(
        reason="integration tests disabled (set BRAIN_INTEGRATION_TESTS=1 to run)"
    )
    for item in items:
        if "integration" in item.keywords or "e2e" in item.keywords:
            item.add_marker(skip_integration)
