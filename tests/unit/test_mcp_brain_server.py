"""Unit tests for brain_mcp_server stdio transport.

Spawns the MCP server as a subprocess, sends initialize + tools/list, and
verifies all 11 brain_* tools are exposed with valid schemas.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

BRAIN_ROOT = Path(__file__).resolve().parents[2]
MCP_SERVER = BRAIN_ROOT / "brain_mcp_server.py"
VENV_PY = BRAIN_ROOT / ".venv/bin/python3"


EXPECTED_TOOLS = {
    "brain_recall",
    "brain_store",
    "brain_decide",
    "brain_reason",
    "brain_ingest",
    "brain_focus",
    "brain_message",
    "brain_changes",
    "brain_evolution",
    "brain_procedures",
    "brain_outcome",
    "brain_search_web",  # Phase M6: SearXNG-backed web search with brain learning
    # Phase 5: working memory session API
    "brain_wm_set",
    "brain_wm_get",
    "brain_wm_list",
    # v3 vision support
    "brain_ingest_image",
    # 2026-04-16 Tier 3 #8: cognitive verbs
    "brain_forget",
    "brain_consolidate",
    "brain_doubt",
}


def _send_jsonrpc(requests: list[dict]) -> list[dict]:
    """Spawn the MCP server, send a sequence of JSON-RPC frames, capture replies."""
    payload = "".join(json.dumps(r) + "\n" for r in requests)
    proc = subprocess.Popen(
        [str(VENV_PY), str(MCP_SERVER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        out, _err = proc.communicate(payload, timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _err = proc.communicate()
    replies = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            replies.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return replies


def test_initialize_returns_server_info():
    replies = _send_jsonrpc(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1.0"},
                },
            }
        ]
    )
    init = next((r for r in replies if r.get("id") == 1), None)
    assert init is not None, "no initialize response"
    server_info = init["result"]["serverInfo"]
    assert server_info["name"] == "brain"
    assert "version" in server_info


def test_tools_list_exposes_all_brain_tools():
    replies = _send_jsonrpc(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1.0"},
                },
            },
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
    )
    list_reply = next((r for r in replies if r.get("id") == 2), None)
    assert list_reply is not None, "no tools/list response"
    tools = list_reply["result"]["tools"]
    names = {t["name"] for t in tools}
    missing = EXPECTED_TOOLS - names
    extra = names - EXPECTED_TOOLS
    assert not missing, f"missing tools: {missing}"
    assert not extra, f"unexpected tools (drift?): {extra}"
    assert len(tools) == len(EXPECTED_TOOLS)


def test_tools_have_valid_input_schema():
    replies = _send_jsonrpc(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1.0"},
                },
            },
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
    )
    list_reply = next((r for r in replies if r.get("id") == 2), None)
    for tool in list_reply["result"]["tools"]:
        assert "name" in tool
        assert "description" in tool
        assert "inputSchema" in tool
        schema = tool["inputSchema"]
        assert schema.get("type") == "object"
        assert "properties" in schema


# ── Timeout cap coverage — ensures slow/LLM/network tools pass timeout_s=4 ──
#
# MCP transport (OpenClaw bundle-mcp) enforces a 5s operation timeout. Any
# brain tool that can exceed 5s MUST pass timeout_s=4 to _brain_request so the
# HTTP layer returns before MCP raises -32001. This test statically verifies
# the source code, catching regressions where a new slow tool is added without
# the cap. See gateway.err.log 2026-04-20T13:19 for the Ellie brain_search_web
# incident that prompted this coverage.

import re  # noqa: E402

MCP_SOURCE = MCP_SERVER.read_text()

# Tools that MUST be capped because they can exceed 5s:
#   - LLM-backed: decide, reason, ingest (classify+embed), store (cold path)
#   - Network-backed: search_web (searxng)
#   - Vision-LLM: ingest_image (Gemini)
#   - Heavy compute: consolidate (full pass)
TIMEOUT_CAPPED_TOOLS = {
    "brain_decide",
    "brain_reason",
    "brain_ingest",
    "brain_ingest_image",
    "brain_search_web",
    "brain_store",
    "brain_consolidate",
}


def _branch_body(tool_name: str) -> str:
    """Extract the source code of the `elif name == "<tool>":` branch."""
    marker = f'elif name == "{tool_name}":'
    start = MCP_SOURCE.find(marker)
    assert start != -1, f"branch for {tool_name} not found in brain_mcp_server.py"
    # Next elif / else / top-level statement
    rest = MCP_SOURCE[start + len(marker) :]
    end_match = re.search(r"\n    (?:elif name ==|else:)", rest)
    end = end_match.start() if end_match else len(rest)
    return rest[:end]


def test_slow_tools_pass_timeout_s_4():
    """Every tool in TIMEOUT_CAPPED_TOOLS must pass timeout_s=4 to _brain_request."""
    for tool in TIMEOUT_CAPPED_TOOLS:
        body = _branch_body(tool)
        assert "timeout_s=4" in body, (
            f"{tool}: missing timeout_s=4 cap. MCP transport has 5s limit; "
            f"this tool can exceed it. Add timeout_s=4 to the _brain_request call."
        )


def test_slow_tools_return_structured_timeout_hint():
    """Capped tools must wrap _brain_request in try/except and return a
    structured {"status": "timeout", "hint": ...} instead of letting the
    exception bubble."""
    for tool in TIMEOUT_CAPPED_TOOLS:
        body = _branch_body(tool)
        assert "try:" in body, f"{tool}: no try/except around _brain_request"
        assert '"status": "timeout"' in body, (
            f"{tool}: missing structured timeout response. Agents need "
            f'{{"status": "timeout", "hint": ...}} so they can retry intelligently.'
        )
        assert '"hint":' in body, f"{tool}: missing hint field in timeout response"


def test_no_new_uncapped_tools_added():
    """If a new `elif name == "brain_X":` branch is added and it can be slow,
    it MUST be in TIMEOUT_CAPPED_TOOLS. This test catches drift: a future
    contributor adding brain_heavy_thing without the cap."""
    branch_names = set(re.findall(r'elif name == "(brain_[a-z_]+)":', MCP_SOURCE))
    # Tools known to be fast (pure SQLite / instant): these don't need a cap
    FAST_TOOLS = {
        "brain_recall",  # ChromaDB + rerank, p99 ~1.58s (safe margin)
        "brain_focus",
        "brain_message",
        "brain_changes",
        "brain_evolution",
        "brain_procedures",
        "brain_outcome",
        "brain_wm_set",
        "brain_wm_get",
        "brain_wm_list",
        "brain_forget",
        "brain_doubt",
    }
    unclassified = branch_names - TIMEOUT_CAPPED_TOOLS - FAST_TOOLS
    assert not unclassified, (
        f"new MCP tool(s) not classified for timeout: {unclassified}. "
        f"Add each to either TIMEOUT_CAPPED_TOOLS (slow, needs timeout_s=4) or "
        f"FAST_TOOLS (pure SQLite / instant) in this test file."
    )
