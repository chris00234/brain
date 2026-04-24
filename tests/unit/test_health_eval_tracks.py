"""Unit tests for health-route eval history selection."""

from __future__ import annotations

import json
import sys
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))

from routes.health import _eval_age_hours, _latest_eval_summary, _latest_eval_tracks  # noqa: E402


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")


def test_latest_eval_tracks_reads_stable_extended_before_legacy(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    _write_jsonl(
        logs / "eval-history.jsonl",
        [{"timestamp": "2026-04-15T15:53:07", "track": "default", "accuracy": 69.3}],
    )
    _write_jsonl(
        logs / "eval-history-stable.jsonl",
        [{"timestamp": "2026-04-23T03:31:12", "accuracy": 96.4}],
    )
    _write_jsonl(
        logs / "eval-history-extended.jsonl",
        [{"timestamp": "2026-04-23T03:56:30", "accuracy": 75.7}],
    )

    tracks = _latest_eval_tracks(logs)
    assert tracks["stable"]["track"] == "stable"
    assert tracks["extended"]["track"] == "extended"
    assert tracks["legacy"]["track"] == "default"
    assert _latest_eval_summary(tracks)["track"] == "extended"


def test_eval_age_hours_returns_none_for_bad_timestamp():
    assert _eval_age_hours({"timestamp": "not-a-date"}) is None
