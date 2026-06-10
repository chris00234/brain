"""Stale OpenClaw workspace-instruction classifiers for recall governance.

These helpers identify retired per-agent OpenClaw instruction docs that should
only survive retrieval for OpenClaw/agent-targeted queries.
"""

from __future__ import annotations

import re

from .source_authority import result_metadata

OPENCLAW_WORKSPACE_INSTRUCTION_RE = re.compile(
    r"\.openclaw/workspace-[^/]+/(?:agents|tools)\.md\b", re.IGNORECASE
)
OPENCLAW_QUERY_TOKENS = frozenset(
    {
        "openclaw",
        "오픈클로",
        "jenna",
        "liz",
        "ellie",
        "sage",
        "market",
        "제나",
        "리즈",
        "엘리",
        "세이지",
        "마켓",
        "agent",
        "agents",
        "workspace",
        "에이전트",
    }
)


def is_openclaw_workspace_instruction_result(result: dict) -> bool:
    meta = result_metadata(result)
    path = str(result.get("path") or meta.get("source_path") or meta.get("path") or "")
    return bool(OPENCLAW_WORKSPACE_INSTRUCTION_RE.search(path))
