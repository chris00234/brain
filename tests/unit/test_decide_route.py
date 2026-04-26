from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain_core"))

from brain_core.routes.decide import DecideRequest, _decision_situation, _decision_subject  # noqa: E402


def test_decide_request_context_is_included_in_evaluation_situation() -> None:
    req = DecideRequest(
        situation="Choose the next brain quality improvement.",
        context="Avoid extra API cost and permanent server load.",
        options=[{"label": "ledger"}, {"label": "new daemon"}],
    )

    combined = _decision_situation(req)

    assert req.situation in combined
    assert req.context in combined


def test_decision_subject_changes_when_context_changes() -> None:
    base = DecideRequest(
        situation="Choose the next brain quality improvement.",
        options=[{"label": "ledger"}, {"label": "new daemon"}],
    )
    with_context = DecideRequest(
        situation="Choose the next brain quality improvement.",
        context="No standing daemon.",
        options=[{"label": "ledger"}, {"label": "new daemon"}],
    )

    assert _decision_subject(base) != _decision_subject(with_context)
