"""CLI-first LLM helpers for ingest adapters."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "brain_core"))


def dispatch_json(
    *,
    agent: str,
    prompt: str,
    timeout: int,
    log_failure: Callable[[str], None] | None = None,
    source: str = "ingest",
    thinking: str = "off",
) -> dict | None:
    """Run a JSON-only prompt through cli_llm with OpenClaw only as fallback."""

    try:
        from cli_llm import dispatch

        result = dispatch(
            agent=agent,
            message=prompt,
            thinking=thinking,
            timeout=timeout,
            openclaw_agent=agent,
            backlog_kind="distill",
            backlog_payload={"source": source, "agent": agent, "prompt": prompt},
        )
    except Exception as exc:
        if log_failure:
            log_failure(f"{agent} cli dispatch raised: {str(exc)[:300]}")
        return None

    if not result or not getattr(result, "ok", False):
        if log_failure:
            log_failure(f"{agent} cli dispatch failed: {str(getattr(result, 'error', 'unknown'))[:300]}")
        return None

    text = str(getattr(result, "text", "") or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        if log_failure:
            log_failure(f"could not parse {agent} reply: {exc}")
        return None
