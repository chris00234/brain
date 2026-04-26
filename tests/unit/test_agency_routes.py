from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain_core"))

from brain_core import belief_state  # noqa: E402
from brain_core.routes import agency  # noqa: E402


def test_brain_state_failure_logs_reason_and_route(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def fail_build_belief_state(*, limit: int) -> dict:
        raise RuntimeError(f"state failed at limit={limit}")

    def capture_log_failure(reason: str, route: str = "?") -> None:
        calls.append((reason, route))

    monkeypatch.setattr(belief_state, "build_belief_state", fail_build_belief_state)
    monkeypatch.setattr(agency, "_log_failure", capture_log_failure)

    with pytest.raises(HTTPException):
        agency.get_brain_state(limit=7)

    assert calls == [("state failed at limit=7", "/brain/state")]
