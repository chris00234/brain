from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core"))

import recall_judge


def test_recall_judge_actor_whitelist_includes_codex() -> None:
    assert "codex" in recall_judge.JUDGED_ACTORS
    assert "eval" not in recall_judge.JUDGED_ACTORS
    assert "recall_judge" not in recall_judge.JUDGED_ACTORS
