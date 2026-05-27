"""tests/unit/test_brain_loop_wake_reap.py — wake-tick subprocess reaping.

Regression: 2026-05-26 a child PID (47734) showed up as <defunct> with PPID
matching the long-running brain server. Root cause: _wake_debounced_tick
launched a detached `start_new_session=True` Popen but never installed a
reaper. The only reap path was `poll()` at the next wake event; if no
further wake fired (or a fire was debounced), the previous child stayed
defunct in the brain server's process table.

Fix: spawn a daemon thread calling proc.wait() on every spawn, so each
detached tick is reaped as soon as it exits, without depending on a
subsequent wake.

Run:
  .venv/bin/python -m pytest tests/unit/test_brain_loop_wake_reap.py -q
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core"))

import brain_loop


def test_reap_wake_child_blocks_until_child_exits():
    proc = subprocess.Popen(["/usr/bin/true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    brain_loop._reap_wake_child(proc)
    # wait() returned → child reaped → returncode set, no zombie left.
    assert proc.returncode == 0


def test_reap_wake_child_swallows_exceptions():
    class BadProc:
        def wait(self):
            raise OSError("already reaped")

    # Must not raise even if the proc handle is stale or invalid; a raise
    # inside the daemon would silently kill the reaper for that spawn.
    brain_loop._reap_wake_child(BadProc())


def test_wake_debounced_tick_spawns_and_reaps_without_next_wake(monkeypatch):
    """The full integration: a single wake spawns a child AND reaps it on
    its own, without requiring a second wake to call poll()."""

    # Reset module state so the 3s debounce doesn't skip this call.
    monkeypatch.setattr(brain_loop, "_wake_last_tick_ts", 0.0, raising=False)
    monkeypatch.setattr(brain_loop, "_wake_child_proc", None, raising=False)

    real_popen = subprocess.Popen

    def fast_popen(args, **kwargs):
        # Replace the heavy brain_loop bootstrap with /bin/true. Drop
        # start_new_session so the test parent can still reap predictably.
        kwargs.pop("start_new_session", None)
        return real_popen(
            ["/usr/bin/true"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **{k: v for k, v in kwargs.items() if k in {"env"}},
        )

    monkeypatch.setattr(subprocess, "Popen", fast_popen)

    # Track the reaper daemon so we can join it deterministically.
    started: list[threading.Thread] = []
    real_thread_start = threading.Thread.start

    def tracked_start(self):
        if self.name == "brain_wake_child_reaper":
            started.append(self)
        real_thread_start(self)

    monkeypatch.setattr(threading.Thread, "start", tracked_start)

    brain_loop._wake_debounced_tick(loop=None)  # type: ignore[arg-type]

    proc = brain_loop._wake_child_proc
    assert proc is not None, "wake tick should have spawned a subprocess"
    assert started, "wake tick should have started a reaper daemon"

    reaper = started[0]
    assert reaper.daemon is True
    reaper.join(timeout=2.0)
    assert not reaper.is_alive(), "reaper should exit promptly after child"
    # The reaper thread's wait() set returncode — no need for a subsequent
    # wake event's poll() to reap. This is the property the zombie fix gives.
    assert proc.returncode is not None
    assert proc.returncode == 0


def test_wake_debounced_tick_debounce_skips_within_window(monkeypatch):
    """The debounce path returns BEFORE spawning, so it must not start a
    reaper thread for a non-existent child."""

    monkeypatch.setattr(brain_loop, "_wake_last_tick_ts", time.time(), raising=False)
    monkeypatch.setattr(brain_loop, "_wake_child_proc", None, raising=False)

    spawned = []

    def boom_popen(*a, **kw):
        spawned.append((a, kw))
        raise AssertionError("Popen should not be called during debounce skip")

    monkeypatch.setattr(subprocess, "Popen", boom_popen)
    brain_loop._wake_debounced_tick(loop=None)  # type: ignore[arg-type]
    assert spawned == []
