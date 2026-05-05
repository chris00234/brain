from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ingest"))

import kuma_heartbeats  # noqa: E402


def test_read_creds_supports_json_credential_file(monkeypatch, tmp_path):
    cred = tmp_path / "kuma.json"
    cred.write_text('{"url":"https://kuma.example","username":"alice","password":"secret"}')
    monkeypatch.delenv("KUMA_URL", raising=False)
    monkeypatch.delenv("KUMA_USER", raising=False)
    monkeypatch.delenv("KUMA_PASS", raising=False)
    monkeypatch.setenv("KUMA_CRED_FILE", str(cred))

    assert kuma_heartbeats._read_creds() == ("https://kuma.example", "alice", "secret")


def test_read_creds_keeps_one_line_password_file(monkeypatch, tmp_path):
    cred = tmp_path / "kuma-pass"
    cred.write_text("secret\n")
    monkeypatch.setenv("KUMA_URL", "https://kuma.example")
    monkeypatch.setenv("KUMA_USER", "alice")
    monkeypatch.delenv("KUMA_PASS", raising=False)
    monkeypatch.setenv("KUMA_CRED_FILE", str(cred))

    assert kuma_heartbeats._read_creds() == ("https://kuma.example", "alice", "secret")
