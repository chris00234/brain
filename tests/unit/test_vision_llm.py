"""Unit tests for vision_llm backend policy."""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))

import vision_llm  # noqa: E402


def test_default_backend_is_subscription_cli():
    assert vision_llm.backend_name() == "codex_cli"


def test_is_configured_uses_codex_cli(monkeypatch):
    monkeypatch.setattr(vision_llm, "DEFAULT_BACKEND", "codex_cli")
    monkeypatch.setattr(vision_llm.shutil, "which", lambda name: "/opt/homebrew/bin/codex")
    assert vision_llm.is_configured() is True


def test_gemini_requires_explicit_backend_and_key(monkeypatch):
    monkeypatch.setattr(vision_llm, "DEFAULT_BACKEND", "gemini")
    monkeypatch.setattr(vision_llm, "_load_api_key", lambda: "")
    assert vision_llm.is_configured() is False
    monkeypatch.setattr(vision_llm, "_load_api_key", lambda: "key")
    assert vision_llm.is_configured() is True


def test_describe_with_codex_uses_image_flag(monkeypatch, tmp_path):
    image = tmp_path / "image.png"
    image.write_bytes(b"fakepng")
    seen: dict[str, list[str]] = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        out_path = Path(cmd[cmd.index("--output-last-message") + 1])
        out_path.write_text("caption text")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(vision_llm.shutil, "which", lambda name: "/opt/homebrew/bin/codex")
    monkeypatch.setattr(vision_llm.subprocess, "run", fake_run)

    caption, error = vision_llm._describe_with_codex(image, "describe")
    assert caption == "caption text"
    assert error == ""
    assert seen["cmd"][:2] == ["/opt/homebrew/bin/codex", "exec"]
    assert "--image" in seen["cmd"]
    assert str(image) in seen["cmd"]


def test_describe_with_codex_enforces_concurrency(monkeypatch, tmp_path):
    image = tmp_path / "image.png"
    image.write_bytes(b"fakepng")
    sem = threading.BoundedSemaphore(1)
    sem.acquire()
    monkeypatch.setattr(vision_llm, "_cli_semaphore", sem)
    monkeypatch.setattr(vision_llm.shutil, "which", lambda name: "/opt/homebrew/bin/codex")

    caption, error = vision_llm._describe_with_codex(image, "describe")
    assert caption == ""
    assert error == "codex_busy"


def test_describe_image_bytes_uses_codex_backend(monkeypatch):
    monkeypatch.setattr(vision_llm, "DEFAULT_BACKEND", "codex_cli")
    monkeypatch.setattr(vision_llm, "_count_today_calls", lambda: 0)
    monkeypatch.setattr(vision_llm, "_breaker_allows_request", lambda kind: True)
    monkeypatch.setattr(vision_llm, "_record_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(vision_llm, "_record_usage", lambda *args, **kwargs: None)
    monkeypatch.setattr(vision_llm, "_record_breaker_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(vision_llm, "_describe_with_codex", lambda path, prompt: ("caption", ""))

    assert vision_llm.describe_image(b"fakepng") == "caption"
