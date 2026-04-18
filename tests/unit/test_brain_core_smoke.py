"""Universal import smoke test for brain_core/.

Catches syntax errors, broken imports, and hard-fail module initializers
across every brain_core module. Does NOT verify behavior — that's what
targeted test_*.py files are for. This is the minimum-viable regression
gate: if a single module fails to import, the brain server won't boot.

Maintained 2026-04-17+: before this file, 96 of ~101 modules had zero
test coverage. Any refactor could silently break them and only surface
when the server tried to boot.

Adding a new module? Just drop it in brain_core/ — this test
auto-discovers it. Excluded: brain_core/__init__.py.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

BRAIN_CORE = Path(__file__).resolve().parents[2] / "brain_core"
sys.path.insert(0, str(BRAIN_CORE))
sys.path.insert(0, str(BRAIN_CORE / "pipeline"))


def _discover_modules() -> list[str]:
    """Find all brain_core/*.py and brain_core/pipeline/*.py modules."""
    mods: list[str] = []
    for p in sorted(BRAIN_CORE.glob("*.py")):
        if p.name == "__init__.py":
            continue
        mods.append(p.stem)
    for p in sorted((BRAIN_CORE / "pipeline").glob("*.py")):
        if p.name == "__init__.py":
            continue
        mods.append(p.stem)
    return mods


# Modules that intentionally depend on live services (Ollama / ChromaDB /
# Neo4j) and would fail import in a pure-unit test environment. Exclude
# from the "must import" guarantee but still list them below so we know.
# If one of these crashes for other reasons (syntax, missing stdlib), the
# check below still catches it.
_ALLOWED_FAILURES_ON_IMPORT: set[str] = set()


@pytest.mark.parametrize("modname", _discover_modules())
def test_module_imports(modname: str) -> None:
    """Every brain_core module must import cleanly at test time."""
    try:
        importlib.import_module(modname)
    except ImportError as exc:
        # Only accept ImportError if it's in our known-flaky list
        if modname in _ALLOWED_FAILURES_ON_IMPORT:
            pytest.skip(f"{modname}: known live-service dep ({exc})")
        raise
    except SyntaxError:
        pytest.fail(f"{modname}: SYNTAX ERROR — server won't boot")
