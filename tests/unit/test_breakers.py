"""Unit tests for brain_core.breakers — persistent circuit breaker."""

from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))


@pytest.fixture
def isolated_breakers(tmp_path, monkeypatch):
    """Point breakers at a fresh tmp_path autonomy.db."""
    if "breakers" in sys.modules:
        del sys.modules["breakers"]
    if "config" in sys.modules:
        del sys.modules["config"]
    import breakers

    fake_db = tmp_path / "autonomy.db"
    monkeypatch.setattr(breakers, "AUTONOMY_DB", fake_db)
    monkeypatch.setattr(breakers, "_initialized", False)
    breakers._snapshot_cache.clear()
    yield breakers
    importlib.reload(breakers)


def test_baseline_breaker_is_closed(isolated_breakers):
    snap = isolated_breakers.peek_breaker("test.kind")
    assert snap.state == "closed"
    assert snap.failures == 0
    assert snap.is_closed
    assert not snap.is_open


def test_three_failures_open_breaker(isolated_breakers):
    for _ in range(3):
        isolated_breakers.record_result("test.kind", ok=False, error="boom")
    snap = isolated_breakers.peek_breaker("test.kind")
    assert snap.state == "open"
    assert snap.failures == 3
    assert snap.trip_count == 1


def test_success_resets_breaker(isolated_breakers):
    isolated_breakers.record_result("test.kind", ok=False)
    isolated_breakers.record_result("test.kind", ok=False)
    isolated_breakers.record_result("test.kind", ok=True)
    snap = isolated_breakers.peek_breaker("test.kind")
    assert snap.state == "closed"
    assert snap.failures == 0


def test_open_breaker_auto_promotes_to_half_open_after_cooldown(isolated_breakers, monkeypatch):
    # Force open
    for _ in range(3):
        isolated_breakers.record_result("test.kind", ok=False)
    isolated_breakers._snapshot_cache.clear()

    # Backdate opened_at to simulate cooldown expired
    import sqlite3

    conn = sqlite3.connect(str(isolated_breakers.AUTONOMY_DB))
    conn.execute(
        "UPDATE heal_breakers SET opened_at = ? WHERE kind = 'test.kind'",
        (time.time() - 10000,),  # 10000s ago
    )
    conn.commit()
    conn.close()
    isolated_breakers._snapshot_cache.clear()

    snap = isolated_breakers.peek_breaker("test.kind")
    assert snap.state == "half_open"


def test_half_open_failure_reopens_with_next_tier(isolated_breakers, monkeypatch):
    # Open once
    for _ in range(3):
        isolated_breakers.record_result("test.kind", ok=False)
    snap = isolated_breakers.peek_breaker("test.kind")
    first_reset = snap.reset_after_s

    # Force half-open
    import sqlite3

    conn = sqlite3.connect(str(isolated_breakers.AUTONOMY_DB))
    conn.execute("UPDATE heal_breakers SET state = 'half_open' WHERE kind = 'test.kind'")
    conn.commit()
    conn.close()
    isolated_breakers._snapshot_cache.clear()

    isolated_breakers.record_result("test.kind", ok=False, error="probe failed")
    snap = isolated_breakers.peek_breaker("test.kind")
    assert snap.state == "open"
    assert snap.trip_count == 2
    assert snap.reset_after_s > first_reset, "next tier should have a longer cooldown"


def test_reset_clears_breaker(isolated_breakers):
    for _ in range(3):
        isolated_breakers.record_result("test.kind", ok=False)
    isolated_breakers.reset("test.kind")
    snap = isolated_breakers.peek_breaker("test.kind")
    assert snap.state == "closed"
    assert snap.failures == 0
    assert snap.trip_count == 0
    assert snap.reason == "manual_reset"


def test_list_all_returns_all_known_kinds(isolated_breakers):
    isolated_breakers.record_result("kind.one", ok=False)
    isolated_breakers.record_result("kind.two", ok=True)
    isolated_breakers.record_result("kind.three", ok=False)
    all_breakers = isolated_breakers.list_all()
    kinds = {b.kind for b in all_breakers}
    assert kinds == {"kind.one", "kind.two", "kind.three"}


def test_persistence_across_recreation(isolated_breakers):
    for _ in range(3):
        isolated_breakers.record_result("test.kind", ok=False)
    # Simulate a fresh reload — clear in-memory cache + flag
    isolated_breakers._snapshot_cache.clear()
    isolated_breakers._initialized = False
    snap = isolated_breakers.peek_breaker("test.kind")
    assert snap.state == "open", "persistence: state must survive process recycle"


def test_try_claim_probe_single_flight(isolated_breakers):
    """Half_open → exactly one caller wins try_claim_probe."""
    # Force half_open
    import sqlite3

    for _ in range(3):
        isolated_breakers.record_result("probe.kind", ok=False)
    conn = sqlite3.connect(str(isolated_breakers.AUTONOMY_DB))
    conn.execute("UPDATE heal_breakers SET state = 'half_open' WHERE kind = 'probe.kind'")
    conn.commit()
    conn.close()
    isolated_breakers._snapshot_cache.clear()

    # First caller wins
    assert isolated_breakers.try_claim_probe("probe.kind") is True
    # Second caller loses — state was CAS'd to half_open_probing
    assert isolated_breakers.try_claim_probe("probe.kind") is False


def test_half_open_failure_sets_fresh_opened_at(isolated_breakers, monkeypatch):
    """Regression: half_open → failure must re-open with new opened_at + next tier,
    not stale values that immediately auto-promote back to half_open."""
    import sqlite3
    import time as _t

    for _ in range(3):
        isolated_breakers.record_result("loop.kind", ok=False)
    snap1 = isolated_breakers.peek_breaker("loop.kind")
    first_tier = snap1.reset_after_s
    stale_opened_at = snap1.opened_at

    # Simulate cooldown expired → half_open
    conn = sqlite3.connect(str(isolated_breakers.AUTONOMY_DB))
    conn.execute(
        "UPDATE heal_breakers SET state='half_open', opened_at=? WHERE kind='loop.kind'",
        (stale_opened_at,),
    )
    conn.commit()
    conn.close()
    isolated_breakers._snapshot_cache.clear()

    # Record probe failure
    before = _t.time()
    isolated_breakers.record_result("loop.kind", ok=False, error="probe-fail")
    snap2 = isolated_breakers.peek_breaker("loop.kind")

    assert snap2.state == "open"
    assert snap2.trip_count == 2
    assert snap2.reset_after_s > first_tier, "probe failure should escalate backoff tier"
    assert snap2.opened_at is not None and snap2.opened_at >= before, (
        "probe failure should reset opened_at to now, not reuse stale value"
    )


def test_half_open_probing_blocks_new_callers(isolated_breakers):
    """Once a caller has claimed the probe, subsequent peeks see half_open_probing."""
    import sqlite3

    for _ in range(3):
        isolated_breakers.record_result("race.kind", ok=False)
    conn = sqlite3.connect(str(isolated_breakers.AUTONOMY_DB))
    conn.execute("UPDATE heal_breakers SET state='half_open' WHERE kind='race.kind'")
    conn.commit()
    conn.close()
    isolated_breakers._snapshot_cache.clear()

    assert isolated_breakers.try_claim_probe("race.kind") is True
    snap = isolated_breakers.peek_breaker("race.kind")
    assert snap.state == "half_open_probing"
    assert snap.blocks_new_callers is True
    assert snap.is_probing is True
