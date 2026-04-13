"""Unit tests for cli.outbox_drain — SessionEnd transcript outbox drainer."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "cli"))


@pytest.fixture
def isolated_outbox(tmp_path, monkeypatch):
    """Point the drainer at a tmp_path outbox tree."""
    import outbox_drain

    out = tmp_path / "outbox"
    monkeypatch.setattr(outbox_drain, "OUTBOX_ROOT", out)
    monkeypatch.setattr(outbox_drain, "PENDING", out / "pending")
    monkeypatch.setattr(outbox_drain, "INFLIGHT", out / "inflight")
    monkeypatch.setattr(outbox_drain, "DONE", out / "done")
    monkeypatch.setattr(outbox_drain, "QUARANTINE", out / "quarantine")
    monkeypatch.setattr(outbox_drain, "LOG", tmp_path / "drain.log")
    outbox_drain._ensure_dirs()
    yield outbox_drain


def _enqueue(drain_module, sid: str, transcript_path: str = "/tmp/no_such") -> Path:
    envelope = {
        "session_id": sid,
        "transcript_path": transcript_path,
        "enqueued_ts": time.time(),
        "retries": 0,
        "next_attempt_ts": time.time(),
        "schema_version": 1,
    }
    p = drain_module.PENDING / f"{sid}.jsonl"
    p.write_text(json.dumps(envelope) + "\n")
    return p


def test_skip_short_when_no_transcript(isolated_outbox, monkeypatch):
    monkeypatch.setattr(isolated_outbox, "_read_secret", lambda: "fake-secret")
    _enqueue(isolated_outbox, "sid-001")
    isolated_outbox.main()
    assert (isolated_outbox.DONE / "sid-001.jsonl").exists()
    assert not (isolated_outbox.PENDING / "sid-001.jsonl").exists()


def test_no_secret_aborts(isolated_outbox, monkeypatch):
    monkeypatch.setattr(isolated_outbox, "_read_secret", lambda: "")
    _enqueue(isolated_outbox, "sid-002")
    rc = isolated_outbox.main()
    assert rc == 1
    assert (isolated_outbox.PENDING / "sid-002.jsonl").exists(), "envelope must be untouched on no-secret"


def test_deferred_envelope_skipped(isolated_outbox, monkeypatch):
    monkeypatch.setattr(isolated_outbox, "_read_secret", lambda: "fake-secret")
    sid = "sid-deferred"
    envelope = {
        "session_id": sid,
        "transcript_path": "/tmp/no_such",
        "enqueued_ts": time.time(),
        "retries": 0,
        "next_attempt_ts": time.time() + 3600,  # 1h in the future
        "schema_version": 1,
    }
    p = isolated_outbox.PENDING / f"{sid}.jsonl"
    p.write_text(json.dumps(envelope) + "\n")
    isolated_outbox.main()
    assert (isolated_outbox.PENDING / f"{sid}.jsonl").exists(), "deferred should stay in pending"


def test_quarantine_after_max_retries(isolated_outbox, monkeypatch, tmp_path):
    monkeypatch.setattr(isolated_outbox, "_read_secret", lambda: "fake-secret")
    transcript_file = tmp_path / "fake.jsonl"
    # synthesize a long transcript so it doesn't get classified as skip_short
    lines = []
    for i in range(20):
        lines.append(json.dumps({"type": "user", "message": {"content": "x" * 200 + f" iteration {i}"}}))
    transcript_file.write_text("\n".join(lines))

    sid = "sid-quarantine"
    envelope = {
        "session_id": sid,
        "transcript_path": str(transcript_file),
        "enqueued_ts": time.time(),
        "retries": isolated_outbox.MAX_RETRIES - 1,  # one more failure → quarantine
        "next_attempt_ts": time.time(),
        "schema_version": 1,
    }
    p = isolated_outbox.PENDING / f"{sid}.jsonl"
    p.write_text(json.dumps(envelope) + "\n")

    with patch.object(isolated_outbox, "_post_json", return_value=(500, "boom")):
        isolated_outbox.main()

    assert (isolated_outbox.QUARANTINE / f"{sid}.jsonl").exists(), "should be quarantined"
    assert not (isolated_outbox.PENDING / f"{sid}.jsonl").exists()


def test_orphan_inflight_recovered(isolated_outbox, monkeypatch):
    monkeypatch.setattr(isolated_outbox, "_read_secret", lambda: "fake-secret")
    sid = "sid-orphan"
    p = isolated_outbox.INFLIGHT / f"{sid}.jsonl"
    p.write_text(
        json.dumps(
            {"session_id": sid, "transcript_path": "", "retries": 0, "next_attempt_ts": 0, "enqueued_ts": 0}
        )
        + "\n"
    )
    # Backdate mtime so it looks stale (>10 min old).
    old = time.time() - 1200
    import os

    os.utime(p, (old, old))
    isolated_outbox._recover_orphan_inflight()
    assert not (isolated_outbox.INFLIGHT / f"{sid}.jsonl").exists()
    assert (isolated_outbox.PENDING / f"{sid}.jsonl").exists()
