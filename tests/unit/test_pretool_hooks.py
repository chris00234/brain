"""E2E-style tests for brain PreToolUse hook scripts."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parents[2]


def _home_with_secret(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    secret_dir = home / ".openclaw" / "credentials"
    secret_dir.mkdir(parents=True)
    (secret_dir / ".personal_webhook_secret").write_text("test-secret")
    return home


def _fake_curl(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(
        """#!/bin/sh
printf '%s\n' "$@" >> "$CURL_ARGS_FILE"
case "$*" in
  *"/brain/coding_events"*) printf '%s' '{"events":[]}' ;;
  *"/memory"*) printf '%s' '{"status":"stored"}' ;;
  *) printf '%s' '{"results":[{"score":120,"path":"/canonical/brain.md","title":"Brain Rule","content":"---\\nUse brain before acting."}]}' ;;
esac
"""
    )
    curl.chmod(0o755)
    return bin_dir


def test_pretool_nudge_sends_agent_session_and_tool_context(tmp_path: Path):
    args_file = tmp_path / "curl_args.txt"
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(_home_with_secret(tmp_path)),
            "PATH": f"{_fake_curl(tmp_path)}:{env['PATH']}",
            "CURL_ARGS_FILE": str(args_file),
            "BRAIN_AGENT": "codex",
        }
    )
    payload = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(ROOT / "brain_core" / "job_definitions.py")},
        "session_id": "hook-session",
        "cwd": str(ROOT),
    }

    result = subprocess.run(
        ["bash", str(ROOT / "cli" / "pretool_brain_nudge.sh")],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        check=True,
    )

    assert "Brain canonical matches relevant here" in result.stdout
    curl_args = args_file.read_text()
    assert "x-agent: codex" in curl_args
    assert "x-session-id: hook-session" in curl_args
    assert "actor=codex" in curl_args

    recall_url = next(line for line in curl_args.splitlines() if "/recall/v2" in line)
    query = parse_qs(urlparse(recall_url).query)["q"][0]
    decoded = unquote(query)
    assert "job_definitions.py" in decoded
    assert "cwd:brain" in decoded
    assert "ai:codex" in decoded
    assert "tool:Read" in decoded


def test_pretool_enforce_override_audits_with_agent(tmp_path: Path):
    args_file = tmp_path / "curl_args.txt"
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(_home_with_secret(tmp_path)),
            "PATH": f"{_fake_curl(tmp_path)}:{env['PATH']}",
            "CURL_ARGS_FILE": str(args_file),
            "BRAIN_AGENT": "codex",
            "BRAIN_OVERRIDE": "1",
        }
    )
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(Path(env["HOME"]) / ".openclaw" / "credentials" / "token")},
    }

    result = subprocess.run(
        ["bash", str(ROOT / "cli" / "pretool_brain_enforce.sh")],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        check=True,
    )

    assert result.stdout == ""
    curl_args = args_file.read_text()
    assert "/memory" in curl_args
    assert "x-agent: codex" in curl_args
    assert '"agent":"codex"' in curl_args


def test_pretool_enforce_denies_without_override(tmp_path: Path):
    env = os.environ.copy()
    env.update({"HOME": str(_home_with_secret(tmp_path)), "BRAIN_AGENT": "codex"})
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(Path(env["HOME"]) / ".openclaw" / "credentials" / "token")},
    }

    result = subprocess.run(
        ["bash", str(ROOT / "cli" / "pretool_brain_enforce.sh")],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        check=True,
    )

    data = json.loads(result.stdout)
    output = data["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    assert output["permissionDecision"] == "deny"
    assert "OpenClaw credentials dir" in output["permissionDecisionReason"]


def test_codex_boot_suppresses_empty_active_recall(tmp_path: Path):
    """UserPromptSubmit hook should stay silent when /recall/active has no blocks."""
    args_file = tmp_path / "curl_args.txt"
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(_home_with_secret(tmp_path)),
            "PATH": f"{_fake_curl(tmp_path)}:{env['PATH']}",
            "CURL_ARGS_FILE": str(args_file),
        }
    )
    payload = {
        "prompt": "UserPromptSubmit hook 여기서 나오는거",
        "session_id": "codex-session",
        "cwd": str(ROOT),
    }

    result = subprocess.run(
        ["bash", str(ROOT / "cli" / "codex_boot.sh")],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        check=True,
    )

    assert result.stdout == ""
    assert "/recall/active" in args_file.read_text()


def test_codex_boot_truncates_active_recall_payload_to_server_schema(tmp_path: Path):
    args_file = tmp_path / "curl_args.txt"
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(_home_with_secret(tmp_path)),
            "PATH": f"{_fake_curl(tmp_path)}:{env['PATH']}",
            "CURL_ARGS_FILE": str(args_file),
        }
    )
    payload = {
        "prompt": "x" * 9000,
        "session_id": "s" * 200,
        "cwd": "/" + ("deep/" * 200),
    }

    subprocess.run(
        ["bash", str(ROOT / "cli" / "codex_boot.sh")],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        check=True,
    )

    curl_args = args_file.read_text()
    request_body = next(line for line in curl_args.splitlines() if line.startswith('{"prompt"'))
    data = json.loads(request_body)
    assert len(data["prompt"]) == 8000
    assert len(data["session_id"]) == 128
    assert len(data["cwd"]) == 512
