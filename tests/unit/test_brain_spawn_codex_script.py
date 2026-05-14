from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "cli" / "brain-spawn-codex"


def _write_fake_codex(path: Path, marker: str) -> None:
    path.write_text(
        f"""#!/bin/bash
set -euo pipefail
if [ "${{FAKE_CODEX_MODE:-ok}}" = "timeout" ]; then
  {sys.executable} -c 'import time; time.sleep(60)' {marker} &
  wait
else
  cat >/dev/null
  echo "fake codex ok $*"
fi
"""
    )
    path.chmod(0o755)


def _base_env(tmp_path: Path, fake_codex: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "BRAIN_AUTOSPAWN_CODEX": "on",
            "BRAIN_SPAWN_ACK_ENABLED": "off",
            "BRAIN_CODEX_OUTBOX": str(tmp_path / "outbox"),
            "BRAIN_SPAWN_LOG_DIR": str(tmp_path / "logs"),
            "BRAIN_CODEX_BIN": str(fake_codex),
            "BRAIN_SPAWN_MAX_PER_RUN": "1",
            "BRAIN_CODEX_PRIMARY_MODEL": "gpt-5.5",
        }
    )
    return env


@pytest.mark.skipif(shutil.which("jq") is None, reason="brain-spawn-codex requires jq")
def test_brain_spawn_codex_moves_success_to_done(tmp_path: Path):
    marker = f"brain-spawn-codex-test-child-{uuid4().hex}"
    fake_codex = tmp_path / "fake-codex"
    _write_fake_codex(fake_codex, marker)
    outbox = tmp_path / "outbox"
    pending = outbox / "pending"
    pending.mkdir(parents=True)
    (pending / "msg.json").write_text(json.dumps({"message_id": "msg", "content": "hello"}))

    subprocess.run([str(SCRIPT)], env=_base_env(tmp_path, fake_codex), check=True, timeout=20)

    assert not (pending / "msg.json").exists()
    assert (outbox / "done" / "msg.json").exists()
    assert not list((outbox / "failed").glob("*.json"))
    log = (tmp_path / "logs" / "brain_spawn_codex.log").read_text()
    assert "-m gpt-5.5" not in log  # model is passed to Codex, not leaked into logs
    assert "msg=msg ok" in log


@pytest.mark.skipif(shutil.which("jq") is None, reason="brain-spawn-codex requires jq")
def test_brain_spawn_codex_timeout_kills_child_process_group(tmp_path: Path):
    marker = f"brain-spawn-codex-test-child-{uuid4().hex}"
    fake_codex = tmp_path / "fake-codex"
    _write_fake_codex(fake_codex, marker)
    outbox = tmp_path / "outbox"
    pending = outbox / "pending"
    pending.mkdir(parents=True)
    (pending / "msg.json").write_text(json.dumps({"message_id": "msg", "content": "timeout"}))
    env = _base_env(tmp_path, fake_codex)
    env["FAKE_CODEX_MODE"] = "timeout"
    env["BRAIN_SPAWN_CODEX_TIMEOUT_S"] = "1"

    subprocess.run([str(SCRIPT)], env=env, check=True, timeout=20)

    assert not (pending / "msg.json").exists()
    assert (outbox / "failed" / "msg.json").exists()
    ps = subprocess.run(["ps", "-axo", "command="], capture_output=True, text=True, check=True)
    assert marker not in ps.stdout
    log = (tmp_path / "logs" / "brain_spawn_codex.log").read_text()
    assert "msg=msg FAILED rc=124" in log
