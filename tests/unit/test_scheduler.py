"""Unit tests for brain_core/scheduler.py — JOB_SCHEDULE integrity and
the _fire race-guard that prevents duplicate dispatch."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core"))


def test_job_schedule_loads():
    from scheduler import JOB_SCHEDULE
    # We know there are ~108 jobs as of 2026-04-17; assert a floor that
    # fails loudly if someone accidentally drops half the JOB_SCHEDULE.
    assert len(JOB_SCHEDULE) >= 100


def test_job_names_unique():
    from scheduler import JOB_SCHEDULE
    names = [j.name for j in JOB_SCHEDULE]
    assert len(names) == len(set(names)), f"duplicate job names: {[n for n in names if names.count(n) > 1]}"


def test_all_jobs_have_description():
    from scheduler import JOB_SCHEDULE
    missing = [j.name for j in JOB_SCHEDULE if not j.description]
    assert missing == [], f"jobs missing description: {missing}"


def test_hnsw_adaptive_present_hnsw_tune_removed():
    """2026-04-17: dedupe removed hnsw_tune; hnsw_adaptive still scheduled."""
    from scheduler import JOB_SCHEDULE
    names = {j.name for j in JOB_SCHEDULE}
    assert "hnsw_adaptive" in names
    assert "hnsw_tune" not in names


def test_misfire_grace_default_not_huge():
    """Regression: class DEFAULT was 3600, caused thundering herd after
    restart. Default dropped to 300 on 2026-04-16. Individual heavy
    jobs may override up to 3600. We assert the default, not every
    entry, so explicit overrides (weekly heavy batch jobs that benefit
    from a longer replay window) remain allowed."""
    from scheduler import ScheduledJob
    import inspect

    # Read the default from the dataclass definition itself.
    default = inspect.signature(ScheduledJob).parameters["misfire_grace"].default
    assert default <= 900, f"default misfire_grace is {default} — must be ≤ 900"


def test_session_rotate_scheduled():
    """2026-04-17: session_rotate cron added as weekly Sunday job."""
    from scheduler import JOB_SCHEDULE
    match = [j for j in JOB_SCHEDULE if j.name == "session_rotate"]
    assert len(match) == 1


def test_fire_guard_blocks_duplicate():
    """Regression: scheduled _fire() must skip when _running_jobs already
    has a live PID for the same job (2026-04-17 race-fix)."""
    from scheduler import BrainScheduler

    sched = BrainScheduler()
    sched._dispatcher = MagicMock(return_value=99999)

    # First fire — dispatcher called
    import os as _os
    orig_kill = _os.kill
    _os.kill = lambda pid, sig: None  # pid=99999 "alive"
    try:
        sched._fire("test_job")
        assert sched._dispatcher.call_count == 1
        # Second fire while _running_jobs["test_job"] = 99999 still alive
        sched._fire("test_job")
        # Dispatcher should NOT have been called again
        assert sched._dispatcher.call_count == 1
    finally:
        _os.kill = orig_kill


def test_fire_guard_clears_stale_entry():
    """When the _running_jobs PID is dead, guard clears it and proceeds."""
    from scheduler import BrainScheduler

    sched = BrainScheduler()
    sched._dispatcher = MagicMock(return_value=88888)
    sched._running_jobs["stale_job"] = 11111  # stale

    import os as _os
    orig_kill = _os.kill

    def _kill(pid, sig):
        if pid == 11111:
            raise ProcessLookupError("gone")
        return None

    _os.kill = _kill
    try:
        sched._fire("stale_job")
        # Dispatcher fired because the stale PID was cleaned up
        assert sched._dispatcher.call_count == 1
    finally:
        _os.kill = orig_kill
