"""Unit tests for brain_core/scheduler.py — JOB_SCHEDULE integrity and
the _fire race-guard that prevents duplicate dispatch."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

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
    import inspect

    from scheduler import ScheduledJob

    # Read the default from the dataclass definition itself.
    default = inspect.signature(ScheduledJob).parameters["misfire_grace"].default
    assert default <= 900, f"default misfire_grace is {default} — must be ≤ 900"


def test_session_rotate_scheduled():
    """2026-04-17: session_rotate cron added as weekly Sunday job."""
    from scheduler import JOB_SCHEDULE

    match = [j for j in JOB_SCHEDULE if j.name == "session_rotate"]
    assert len(match) == 1


def test_scheduled_jobs_have_registry_entries():
    """Every scheduled job must be dispatchable by JOB_REGISTRY."""
    from job_registry import JOB_REGISTRY
    from scheduler import JOB_SCHEDULE

    missing = sorted(j.name for j in JOB_SCHEDULE if j.name not in JOB_REGISTRY)
    assert missing == []


def test_extended_eval_job_uses_v2_set_and_loose_metric():
    """Extended trend track should follow current eval-set and semantic content metric."""
    from job_registry import JOB_REGISTRY

    cmd = JOB_REGISTRY["eval_run_extended"]
    assert any("eval_set_extended_v2.json" in part for part in cmd)
    assert "--content-metric" in cmd
    assert "loose" in cmd


def test_near_dedup_does_not_collide_with_sm2_nightly():
    """Both jobs touch memory stores; keep them off the same exact minute."""
    from scheduler import JOB_SCHEDULE

    by_name = {j.name: str(j.trigger) for j in JOB_SCHEDULE}
    assert "near_dedup" in by_name
    assert "sm2_nightly" in by_name
    assert by_name["near_dedup"] != by_name["sm2_nightly"]
    assert "minute='22'" in by_name["near_dedup"]


def test_review_jobs_are_staggered_from_competing_heavy_jobs():
    """Review/LLM judge jobs should not exact-collide with other heavy jobs."""
    from scheduler import JOB_SCHEDULE

    by_name = {j.name: str(j.trigger) for j in JOB_SCHEDULE}
    assert by_name["self_eval"] != by_name["code_index_refresh"]
    assert "minute='37'" in by_name["self_eval"]
    assert by_name["recall_judge"] != by_name["eval_proposal_triage"]
    assert "minute='27'" in by_name["recall_judge"]


def test_heavy_jobs_have_resource_budgets():
    """Heavy recurring jobs should carry machine-readable resource labels."""
    from scheduler import JOB_SCHEDULE

    by_name = {j.name: j for j in JOB_SCHEDULE}
    assert by_name["reindex"].resource_class == "heavy"
    assert {"embedder", "qdrant", "index"} <= set(by_name["reindex"].resource_tags)
    assert by_name["recall_judge"].resource_class == "heavy"
    assert {"llm", "qdrant"} <= set(by_name["recall_judge"].resource_tags)
    assert by_name["brain_loop_tick"].resource_class == "standard"


def test_scheduler_defers_scheduled_job_when_resource_budget_full(tmp_path):
    """Scheduled heavy jobs defer instead of overlapping routine heavy load."""
    from scheduler import BrainScheduler

    sched = BrainScheduler(db_path=tmp_path / "test_scheduler.db")
    sched._dispatcher = MagicMock(return_value=99999)
    sched._defer_job = MagicMock()
    sched._running_jobs["reindex"] = 12345

    import os as _os

    orig_kill = _os.kill
    _os.kill = lambda pid, sig: None
    try:
        sched._fire("eval_run")
    finally:
        _os.kill = orig_kill

    sched._dispatcher.assert_not_called()
    sched._defer_job.assert_called_once()
    assert sched._defer_job.call_args.args[0] == "eval_run"


def test_scheduler_resource_status_reports_usage_and_defers(tmp_path):
    """Resource budget state should be visible to health/jobs callers."""
    from scheduler import BrainScheduler

    sched = BrainScheduler(db_path=tmp_path / "test_scheduler.db")
    sched._running_jobs["reindex"] = 12345

    import os as _os

    orig_kill = _os.kill
    _os.kill = lambda pid, sig: None
    try:
        status = sched.resource_status()
    finally:
        _os.kill = orig_kill

    assert status["usage"]["heavy"]["running"] == 1
    assert status["usage"]["embedder"]["running"] == 1
    assert "reindex" in status["usage"]["index"]["running_jobs"]


def test_scheduler_records_resource_defer_metadata(tmp_path):
    """A scheduled defer should leave inspectable retry metadata."""
    from scheduler import BrainScheduler

    sched = BrainScheduler(db_path=tmp_path / "test_scheduler.db")
    sched._defer_job("eval_run", "heavy:1/1")

    status = sched.resource_status()
    retry = status["pending_retries"]["eval_run"]
    assert retry["reason"] == "heavy:1/1"
    assert retry["count"] == 1
    listed = next(job for job in sched.list_jobs() if job["name"] == "eval_run")
    assert listed["resource_defer"]["reason"] == "heavy:1/1"


def test_manual_trigger_fails_fast_when_resource_budget_full(tmp_path):
    """Manual triggers should not silently defer; callers need the blocker."""
    from scheduler import BrainScheduler

    sched = BrainScheduler(db_path=tmp_path / "test_scheduler.db")
    sched._dispatcher = MagicMock(return_value=99999)
    sched._running_jobs["reindex"] = 12345

    import os as _os

    orig_kill = _os.kill
    _os.kill = lambda pid, sig: None
    try:
        with pytest.raises(ValueError, match="resource budget"):
            sched.trigger_now("eval_run")
    finally:
        _os.kill = orig_kill

    sched._dispatcher.assert_not_called()


def test_cron_map_count_matches_schedule():
    """Rendered docs should not silently drift from the authoritative schedule."""
    import re

    from scheduler import JOB_SCHEDULE

    cron_map = Path(__file__).resolve().parents[2] / "CRON_MAP.md"
    text = cron_map.read_text()
    match = re.search(r"\*\*Total jobs\*\*:\s*(\d+)", text)
    assert match is not None
    assert int(match.group(1)) == len(JOB_SCHEDULE)


def test_cron_map_matches_generated_output():
    """CRON_MAP.md is generated from JOB_SCHEDULE, not maintained by hand."""
    import importlib.util

    cron_map = Path(__file__).resolve().parents[2] / "CRON_MAP.md"
    script = Path(__file__).resolve().parents[2] / "cli" / "render_cron_map.py"
    spec = importlib.util.spec_from_file_location("render_cron_map", script)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert cron_map.read_text() == module.render()


def test_fire_guard_blocks_duplicate(tmp_path):
    """Regression: scheduled _fire() must skip when _running_jobs already
    has a live PID for the same job (2026-04-17 race-fix)."""
    from scheduler import BrainScheduler

    sched = BrainScheduler(db_path=tmp_path / "test_scheduler.db")
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


def test_fire_guard_clears_stale_entry(tmp_path):
    """When the _running_jobs PID is dead, guard clears it and proceeds."""
    from scheduler import BrainScheduler

    sched = BrainScheduler(db_path=tmp_path / "test_scheduler.db")
    sched._dispatcher = MagicMock(return_value=88888)
    sched._running_jobs["stale_job"] = 11111  # stale

    import os as _os

    orig_kill = _os.kill

    def _kill(pid, sig):
        if pid == 11111:
            raise ProcessLookupError("gone")
        return

    _os.kill = _kill
    try:
        sched._fire("stale_job")
        # Dispatcher fired because the stale PID was cleaned up
        assert sched._dispatcher.call_count == 1
    finally:
        _os.kill = orig_kill
