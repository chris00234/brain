from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "brain_core"))

import cli_llm


def test_run_cli_process_returns_completed_process():
    result = cli_llm._run_cli_process([sys.executable, "-c", "print('ok')"], timeout=5)

    assert result.returncode == 0
    assert result.stdout.strip() == "ok"


def test_run_cli_process_kills_process_group_on_timeout(monkeypatch):
    killed: list[tuple[int, int]] = []

    class FakeProc:
        pid = 4242
        returncode = None
        calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(["codex"], timeout)
            self.returncode = -9
            return "", "timed out"

    fake_proc = FakeProc()
    monkeypatch.setattr(cli_llm.subprocess, "Popen", lambda *args, **kwargs: fake_proc)
    monkeypatch.setattr(cli_llm.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    try:
        cli_llm._run_cli_process(["codex"], timeout=1)
    except subprocess.TimeoutExpired as exc:
        assert exc.stderr == "timed out"
    else:
        raise AssertionError("expected TimeoutExpired")

    assert killed == [(4242, cli_llm.signal.SIGKILL)]
