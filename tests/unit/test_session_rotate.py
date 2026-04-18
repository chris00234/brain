"""Unit tests for cli/session_rotate.py."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "cli"))

import session_rotate  # noqa: E402


def _make_agents_tree(root: Path) -> None:
    """Build a fake ~/.openclaw/agents structure."""
    jenna = root / "agents" / "jenna" / "sessions"
    jenna.mkdir(parents=True)
    # Live session (small)
    (jenna / "live.jsonl").write_text("{}\n" * 100)
    # Old checkpoint (> 14 days mtime)
    old_cp = jenna / "abc.checkpoint.x.jsonl"
    old_cp.write_text("X" * 10_000)
    old_ts = time.time() - (20 * 86400)
    import os
    os.utime(old_cp, (old_ts, old_ts))
    # Recent checkpoint (< 14 days) — should be kept
    fresh_cp = jenna / "def.checkpoint.y.jsonl"
    fresh_cp.write_text("Y" * 10_000)


def test_rotate_archives_old_checkpoints(tmp_path, monkeypatch):
    _make_agents_tree(tmp_path)
    monkeypatch.setattr(session_rotate, "AGENTS_ROOT", tmp_path / "agents")

    result = session_rotate.rotate(dry_run=False)

    assert result["status"] == "ok"
    assert result["checkpoints_archived"] == 1
    # Old checkpoint file should be gone
    assert not (tmp_path / "agents" / "jenna" / "sessions" / "abc.checkpoint.x.jsonl").exists()
    # Gzipped archive should exist
    archive_dir = tmp_path / "agents" / "jenna" / "sessions" / "archive"
    assert archive_dir.exists()
    # Fresh checkpoint untouched
    assert (tmp_path / "agents" / "jenna" / "sessions" / "def.checkpoint.y.jsonl").exists()


def test_rotate_dry_run_does_not_modify(tmp_path, monkeypatch):
    _make_agents_tree(tmp_path)
    monkeypatch.setattr(session_rotate, "AGENTS_ROOT", tmp_path / "agents")

    result = session_rotate.rotate(dry_run=True)

    assert result["dry_run"] is True
    assert result["checkpoints_archived"] == 1  # would archive 1
    # But nothing actually moved
    assert (tmp_path / "agents" / "jenna" / "sessions" / "abc.checkpoint.x.jsonl").exists()


def test_rotate_detects_oversized_live_session(tmp_path, monkeypatch):
    tree = tmp_path / "agents" / "jenna" / "sessions"
    tree.mkdir(parents=True)
    oversized = tree / "big.jsonl"
    oversized.write_bytes(b"X" * (int(session_rotate.LIVE_SESSION_ALERT_MB * 1024 * 1024) + 1000))

    monkeypatch.setattr(session_rotate, "AGENTS_ROOT", tmp_path / "agents")
    # Stub out send_chris_telegram so we don't hit the real network
    called = []

    class _Stub:
        @staticmethod
        def send_chris_telegram(*args, **kwargs):
            called.append((args, kwargs))
            return True

    sys.modules["telegram_alert"] = _Stub  # type: ignore[assignment]

    result = session_rotate.rotate(dry_run=False)

    assert result["large_live_sessions"] != []
    assert "jenna/big.jsonl" in result["large_live_sessions"][0][0]
    # Alert was attempted
    assert len(called) == 1
