#!/usr/bin/env python3
"""Brain MCP Server — exposes brain API as MCP tools for OpenClaw agents.

Thin wrapper: translates MCP tool calls → HTTP requests to brain FastAPI.
The brain API itself doesn't change — this is an additional access layer.

Usage:
  openclaw mcp set brain '{"command":"python3","args":["/Users/chrischo/server/brain/brain_mcp_server.py"]}'
"""

import json
import os
import sys
import urllib.request

BRAIN_URL = "http://127.0.0.1:8791"
SECRET_FILE = os.path.expanduser("~/.openclaw/credentials/.personal_webhook_secret")

try:
    SECRET = open(SECRET_FILE).read().strip()
except Exception:
    SECRET = ""


def _brain_request(method: str, path: str, body: dict | None = None) -> dict | str:
    """Make an authenticated request to the brain API."""
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(f"{BRAIN_URL}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {SECRET}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            ct = resp.headers.get("content-type", "")
            raw = resp.read().decode()
            if "json" in ct:
                return json.loads(raw)
            return raw
    except Exception as e:
        return {"error": str(e)[:200]}


# MCP protocol implementation (stdio transport)
def handle_initialize(params):
    return {
        "protocolVersion": "2024-11-05",
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "brain", "version": "1.0.0"},
    }


def handle_tools_list(params):
    return {"tools": [
        {
            "name": "brain_recall",
            "description": "Search Chris's knowledge base. Returns ranked results with scores.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "description": "Max results (default 5)", "default": 5},
                    "collection": {"type": "string", "description": "Filter by collection: semantic_memory, canonical, experience, patterns"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "brain_store",
            "description": "Store a memory/fact/preference in the brain.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "The memory to store"},
                    "category": {"type": "string", "enum": ["preference", "fact", "decision", "entity", "other"]},
                    "agent": {"type": "string", "description": "Your agent name"},
                },
                "required": ["content", "category"],
            },
        },
        {
            "name": "brain_decide",
            "description": "Get a preference-grounded decision recommendation from the brain.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "situation": {"type": "string", "description": "The decision context"},
                    "options": {"type": "array", "items": {"type": "object", "properties": {"label": {"type": "string"}, "description": {"type": "string"}}}, "description": "Options to evaluate"},
                    "agent": {"type": "string"},
                },
                "required": ["situation", "options"],
            },
        },
        {
            "name": "brain_reason",
            "description": "Deep multi-step reasoning with evidence from the knowledge base.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The question to analyze"},
                    "agent": {"type": "string"},
                },
                "required": ["question"],
            },
        },
        {
            "name": "brain_ingest",
            "description": "Manually ingest content into the knowledge base for LLM extraction.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Text content to ingest"},
                    "source": {"type": "string", "description": "Source name", "default": "mcp_ingest"},
                },
                "required": ["content"],
            },
        },
        {
            "name": "brain_focus",
            "description": "Set working context (visible to all agents via boot context).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "What you're focused on"},
                    "agent": {"type": "string"},
                },
                "required": ["content"],
            },
        },
        {
            "name": "brain_message",
            "description": "Send a message to another agent via the brain messaging hub.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "from_agent": {"type": "string"},
                    "to_agent": {"type": "string"},
                    "content": {"type": "string"},
                    "message_type": {"type": "string", "enum": ["info", "alert", "handoff", "decision"], "default": "info"},
                },
                "required": ["from_agent", "to_agent", "content"],
            },
        },
    ]}


def handle_tools_call(params):
    name = params.get("name", "")
    args = params.get("arguments", {})

    if name == "brain_recall":
        q = args["query"]
        n = args.get("limit", 5)
        col = args.get("collection", "")
        path = f"/recall/v2?q={urllib.parse.quote(q)}&n={n}&expand=true"
        if col:
            path += f"&collection={col}"
        result = _brain_request("GET", path)

    elif name == "brain_store":
        result = _brain_request("POST", "/memory", {
            "content": args["content"],
            "category": args.get("category", "fact"),
            "agent": args.get("agent", "mcp"),
            "source": "mcp",
        })

    elif name == "brain_decide":
        result = _brain_request("POST", "/brain/decide", {
            "situation": args["situation"],
            "options": args.get("options", []),
            "agent": args.get("agent", "mcp"),
        })

    elif name == "brain_reason":
        result = _brain_request("POST", "/brain/reason", {
            "question": args["question"],
            "agent": args.get("agent", "mcp"),
        })

    elif name == "brain_ingest":
        result = _brain_request("POST", "/brain/ingest", {
            "content": args["content"],
            "source": args.get("source", "mcp_ingest"),
        })

    elif name == "brain_focus":
        result = _brain_request("POST", "/brain/focus", {
            "content": args["content"],
            "category": "focus",
            "agent": args.get("agent", "mcp"),
        })

    elif name == "brain_message":
        result = _brain_request("POST", "/brain/message", {
            "from_agent": args["from_agent"],
            "to_agent": args["to_agent"],
            "content": args["content"],
            "message_type": args.get("message_type", "info"),
            "priority": 5,
        })

    else:
        result = {"error": f"Unknown tool: {name}"}

    text = json.dumps(result, indent=2) if isinstance(result, dict) else str(result)
    return {"content": [{"type": "text", "text": text[:4000]}]}


import urllib.parse

# MCP stdio transport — read JSON-RPC from stdin, write to stdout
def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = msg.get("method", "")
        params = msg.get("params", {})
        msg_id = msg.get("id")

        if method == "initialize":
            result = handle_initialize(params)
        elif method == "tools/list":
            result = handle_tools_list(params)
        elif method == "tools/call":
            result = handle_tools_call(params)
        elif method == "notifications/initialized":
            continue  # no response needed
        else:
            result = {"error": f"Unknown method: {method}"}

        if msg_id is not None:
            response = {"jsonrpc": "2.0", "id": msg_id, "result": result}
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
