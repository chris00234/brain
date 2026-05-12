"""Unit tests for belief_state._compute_per_domain_agency (D5).

Covers the four classification paths:
  - insufficient samples → review_first_closed_loop
  - high override (>15%) → frozen
  - low override (<5%) → propose_and_inform
  - middle band → review_first_closed_loop

Plus overall-aggregation behavior and the empty-DB / unreachable-DB paths.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


def _seed_outcomes(db_path: Path, rows: list[tuple[str, int]]) -> None:
    """Insert outcome rows (domain, chris_override) with today's timestamp."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE outcomes (
                id TEXT PRIMARY KEY,
                domain TEXT,
                chris_override INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            );
            """
        )
        for idx, (domain, override) in enumerate(rows):
            conn.execute(
                "INSERT INTO outcomes (id, domain, chris_override, created_at) "
                "VALUES (?, ?, ?, datetime('now', '-1 days'))",
                (f"o_{idx:04d}", domain, override),
            )
        conn.commit()
    finally:
        conn.close()


def test_graduation_below_5_percent_override(tmp_path):
    from belief_state import _compute_per_domain_agency

    db = tmp_path / "autonomy.db"
    # 100 outcomes, 2 overrides = 2% → propose_and_inform
    rows = [("general", 0)] * 98 + [("general", 1)] * 2
    _seed_outcomes(db, rows)

    out = _compute_per_domain_agency(str(db))
    assert out["domains"]["general"]["level"] == "propose_and_inform"
    assert out["domains"]["general"]["override_pct"] == 2.0
    assert out["overall"] == "propose_and_inform"


def test_frozen_above_15_percent_override(tmp_path):
    from belief_state import _compute_per_domain_agency

    db = tmp_path / "autonomy.db"
    # 100 outcomes, 50 overrides = 50% → frozen
    rows = [("coding", 0)] * 50 + [("coding", 1)] * 50
    _seed_outcomes(db, rows)

    out = _compute_per_domain_agency(str(db))
    assert out["domains"]["coding"]["level"] == "frozen"
    assert out["overall"] == "frozen"


def test_middle_band_stays_review_first(tmp_path):
    from belief_state import _compute_per_domain_agency

    db = tmp_path / "autonomy.db"
    # 100 outcomes, 10 overrides = 10% → review_first_closed_loop
    rows = [("infra", 0)] * 90 + [("infra", 1)] * 10
    _seed_outcomes(db, rows)

    out = _compute_per_domain_agency(str(db))
    assert out["domains"]["infra"]["level"] == "review_first_closed_loop"
    assert out["overall"] == "review_first_closed_loop"


def test_insufficient_samples_stays_review_first(tmp_path):
    from belief_state import _compute_per_domain_agency

    db = tmp_path / "autonomy.db"
    # Only 10 outcomes — below the 50-sample threshold; cannot graduate
    rows = [("rare_domain", 0)] * 10
    _seed_outcomes(db, rows)

    out = _compute_per_domain_agency(str(db))
    assert out["domains"]["rare_domain"]["level"] == "review_first_closed_loop"
    assert out["domains"]["rare_domain"]["total"] == 10


def test_overall_picks_most_cautious_level(tmp_path):
    from belief_state import _compute_per_domain_agency

    db = tmp_path / "autonomy.db"
    # general clean, coding frozen → overall must be frozen
    rows = [("general", 0)] * 100 + [("coding", 1)] * 80 + [("coding", 0)] * 20
    _seed_outcomes(db, rows)

    out = _compute_per_domain_agency(str(db))
    assert out["domains"]["general"]["level"] == "propose_and_inform"
    assert out["domains"]["coding"]["level"] == "frozen"
    assert out["overall"] == "frozen"


def test_missing_db_returns_default(tmp_path):
    from belief_state import _compute_per_domain_agency

    out = _compute_per_domain_agency(str(tmp_path / "no-such-file.db"))
    assert out["domains"] == {}
    assert out["overall"] == "review_first_closed_loop"


def test_none_db_path_returns_default():
    from belief_state import _compute_per_domain_agency

    out = _compute_per_domain_agency(None)
    assert out["domains"] == {}
    assert out["overall"] == "review_first_closed_loop"
