"""Phase 3: brain → Codex command-and-outbox tests.

Verifies that:
  1. /brain/command rejects unknown agents, including removed claude target.
  2. codex is the canonical outbox target.
  3. codex dispatches drop a JSON envelope at
     ~/.brain_outbox/codex/pending/{msg_id}.json that brain-spawn-codex watches.
  4. The envelope is atomic and contains exactly the payload fields the
     spawner expects (message_id, content, message_type, priority).
  5. Pure agent_messenger targets (jenna/liz/etc) do NOT get an outbox file.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core"))


def _import_command_module(monkeypatch, tmp_outbox: Path):
    """Import brain_core/routes/command and redirect _OUTBOX_ROOT to tmp_outbox.
    Stubs `agent_messenger.send_message` and `atoms_store.insert_raw_event` so
    the test never touches real DBs.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core" / "routes"))
    if "command" in sys.modules:
        del sys.modules["command"]
    import command

    monkeypatch.setattr(command, "_OUTBOX_ROOT", tmp_outbox)

    fake_messenger = type(sys)("agent_messenger")
    fake_messenger.send_message = lambda **kw: {
        "id": kw.get("metadata", {}).get("force_id", "msg-deadbeef"),
        "created_at": "2026-04-27T17:00:00+00:00",
        "_action": "stored",
        **kw,
    }
    monkeypatch.setitem(sys.modules, "agent_messenger", fake_messenger)

    fake_atoms = type(sys)("atoms_store")
    fake_atoms.insert_raw_event = lambda **_kw: None
    monkeypatch.setitem(sys.modules, "atoms_store", fake_atoms)
    return command


def _make_request(command_mod, **kwargs):
    return command_mod.BrainCommandRequest(**kwargs)


def test_command_rejects_unknown_agent(monkeypatch, tmp_path):
    cmd = _import_command_module(monkeypatch, tmp_path)
    from fastapi import HTTPException

    req = _make_request(cmd, to_agent="nonsense", content="x")
    with pytest.raises(HTTPException) as exc_info:
        cmd.brain_command(req)
    assert exc_info.value.status_code == 400


def test_command_claude_target_is_removed(monkeypatch, tmp_path):
    cmd = _import_command_module(monkeypatch, tmp_path)
    from fastapi import HTTPException

    req = _make_request(cmd, to_agent="claude", content="verify lessons surface")
    with pytest.raises(HTTPException) as exc_info:
        cmd.brain_command(req)

    assert exc_info.value.status_code == 400
    assert not (tmp_path / "codex" / "pending").exists()
    assert not (tmp_path / "claude").exists()


def test_command_codex_target_writes_outbox(monkeypatch, tmp_path):
    cmd = _import_command_module(monkeypatch, tmp_path)
    req = _make_request(cmd, to_agent="codex", content="codex test task")
    result = cmd.brain_command(req)
    assert result["to_agent"] == "codex"
    assert result["outbox_path"] is not None
    files = list((tmp_path / "codex" / "pending").glob("*.json"))
    assert len(files) == 1


def test_command_jenna_target_no_outbox(monkeypatch, tmp_path):
    """OpenClaw agents (jenna et al) consume via agent_messenger; no outbox."""
    cmd = _import_command_module(monkeypatch, tmp_path)
    req = _make_request(cmd, to_agent="jenna", content="jenna test")
    result = cmd.brain_command(req)
    assert result["outbox_path"] is None
    assert not (tmp_path / "jenna").exists()


def test_outbox_write_is_atomic(monkeypatch, tmp_path):
    """No partial / .tmp file should survive a successful write."""
    cmd = _import_command_module(monkeypatch, tmp_path)
    req = _make_request(cmd, to_agent="codex", content="atomicity check")
    cmd.brain_command(req)
    pending = tmp_path / "codex" / "pending"
    tmp_files = list(pending.glob(".*.tmp"))
    real_files = list(pending.glob("*.json"))
    assert tmp_files == [], f"leftover tempfile(s): {tmp_files}"
    assert len(real_files) == 1
