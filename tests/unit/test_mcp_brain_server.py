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
