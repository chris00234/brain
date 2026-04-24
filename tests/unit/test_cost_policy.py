"""Regression tests for Chris's no-extra-API-cost policy."""

from __future__ import annotations

import sys
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BRAIN_ROOT / "brain_core"))

import vision_llm  # noqa: E402


def test_vision_default_stays_on_subscription_cli():
    assert vision_llm.backend_name() == "codex_cli"


def test_gemini_is_explicit_opt_in_only():
    text = (BRAIN_ROOT / "brain_core" / "vision_llm.py").read_text()
    assert 'os.environ.get("BRAIN_VISION_BACKEND", "codex_cli")' in text
    assert "BRAIN_VISION_BACKEND=gemini" in text


def test_image_ingest_route_does_not_require_gemini_key():
    text = (BRAIN_ROOT / "brain_core" / "routes" / "ingest.py").read_text()
    assert "missing GEMINI_API_KEY" not in text
    assert '"captioned_by": vision_llm.backend_name()' in text


def test_mcp_image_tool_description_does_not_default_to_gemini():
    text = (BRAIN_ROOT / "brain_mcp_server.py").read_text()
    assert "codex_cli by default" in text
    assert "Gemini REST is explicit opt-in only" in text


def test_ollama_is_embedder_only_not_local_llm_generation():
    roots = [BRAIN_ROOT / "brain_core", BRAIN_ROOT / "cli", BRAIN_ROOT / "ingest", BRAIN_ROOT / "pipeline"]
    texts = "\n".join(path.read_text(errors="ignore") for root in roots for path in root.rglob("*.py"))
    assert "/api/generate" not in texts
    assert "/api/chat" not in texts


def test_health_exposes_local_model_policy_as_embedder_only():
    text = (BRAIN_ROOT / "brain_core" / "routes" / "health.py").read_text()
    assert '"llm": "disabled"' in text
    assert '"ollama_role": "embedder_only"' in text
    assert 'services["ollama_embedder"]' in text
    assert 'services["ollama"]' not in text


def test_scheduler_resource_budget_uses_embedder_label_not_ollama_llm_label():
    from scheduler import JOB_SCHEDULE

    tags = {tag for job in JOB_SCHEDULE for tag in job.resource_tags}
    assert "embedder" in tags
    assert "ollama" not in tags
