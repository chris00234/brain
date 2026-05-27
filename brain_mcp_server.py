#!/usr/bin/env python3
"""Brain MCP Server — exposes brain API as MCP tools for Hermes profiles.

Thin wrapper: translates MCP tool calls → HTTP requests to brain FastAPI.
The brain API itself doesn't change — this is an additional access layer.

Usage:
  openclaw mcp set brain '{"command":"python3","args":["/Users/chrischo/server/brain/brain_mcp_server.py"]}'
"""

import json
import os
import select
import signal
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

BRAIN_URL = "http://127.0.0.1:8791"
SECRET_FILE = Path("~/.brain/credentials/.personal_webhook_secret").expanduser()

# 2026-05-20: BRAIN_MCP_PROFILE selects the exposed tool surface.
#   "full"    — 21-tool legacy surface (default, used by OpenClaw + Codex)
#   "minimal" — 5-tool single-provider surface for native interactive agents
#               (Claude Code / Hermes MemoryManager / Codex slim mode).
# Minimal tools: brain_search, brain_remember, brain_correct, brain_think,
# brain_feedback. They map onto the same HTTP routes as their full-surface
# counterparts; the slim names + collapsed mode params reduce prompt overhead
# for agents that only need the canonical-memory contract.
BRAIN_MCP_PROFILE = os.environ.get("BRAIN_MCP_PROFILE", "full").strip().lower()

# 2026-04-16 R-6: secret is read once at module load AND re-read on
# every auth-failure (401) to handle rotation without requiring a full
# process restart. Secret reload makes long-lived Codex sessions safe without
# forcing lifecycle self-reap that closes the MCP transport mid-session.

_SECRET_CACHE: dict = {"value": "", "loaded_at": 0.0}


def _load_secret(force: bool = False) -> str:
    now = time.time()
    if not force and _SECRET_CACHE["value"] and (now - _SECRET_CACHE["loaded_at"] < 600):
        return _SECRET_CACHE["value"]
    try:
        with SECRET_FILE.open() as f:
            val = f.read().strip()
    except Exception:
        val = ""
    _SECRET_CACHE["value"] = val
    _SECRET_CACHE["loaded_at"] = now
    return val


SECRET = _load_secret()


def _brain_request(
    method: str, path: str, body: dict | None = None, actor: str | None = None, timeout_s: int = 60
) -> dict | str:
    """Make an authenticated request to the brain API.

    `actor` (M7-WS8): the calling agent name — propagated as both `x-agent`
    header (preferred) and `?actor=` query param fallback. This feeds the
    `action_audit` table so /brain/usage can show per-agent adoption counts.

    `timeout_s` (2026-04-17): caller-tunable HTTP timeout. Slow paths
    (brain_decide / brain_reason) pass 4s so OpenClaw's 5s MCP transport
    timeout gets a structured timeout response instead of -32001.
    """
    data = json.dumps(body).encode() if body else None
    if actor and method == "GET" and "?" in path:
        path = path + "&actor=" + urllib.parse.quote(actor)
    elif actor and method == "GET":
        path = path + "?actor=" + urllib.parse.quote(actor)

    def _do_request(secret: str) -> object:
        req = urllib.request.Request(f"{BRAIN_URL}{path}", data=data, method=method)  # noqa: S310
        req.add_header("Authorization", f"Bearer {secret}")
        if actor:
            req.add_header("x-agent", actor)
        if data:
            req.add_header("Content-Type", "application/json")
        return urllib.request.urlopen(req, timeout=timeout_s)  # noqa: S310

    try:
        with _do_request(_load_secret()) as resp:
            ct = resp.headers.get("content-type", "")
            raw = resp.read().decode()
            if "json" in ct:
                return json.loads(raw)
            return raw
    except urllib.error.HTTPError as http_err:
        # 2026-04-16 R-6: on 401, force-reload the secret file and retry once
        # so rotation doesn't require an MCP process restart.
        if http_err.code == 401:
            try:
                with _do_request(_load_secret(force=True)) as resp:
                    ct = resp.headers.get("content-type", "")
                    raw = resp.read().decode()
                    if "json" in ct:
                        return json.loads(raw)
                    return raw
            except Exception as e2:
                return {"error": str(e2)[:200]}
        return {"error": f"HTTP {http_err.code}: {str(http_err)[:180]}"}
    except Exception as e:
        return {"error": str(e)[:200]}


# MCP protocol implementation (stdio transport)
def handle_initialize(params: dict) -> dict:
    return {
        "protocolVersion": "2024-11-05",
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "brain", "version": "1.0.0"},
    }


def _minimal_tool_defs() -> list[dict]:
    """5-tool single-provider surface (BRAIN_MCP_PROFILE=minimal).

    Maps onto the same HTTP routes the full surface uses; slim names and
    mode/scope params keep the agent prompt short. brain_correct keeps its
    existing schema — it's already minimal.
    """
    return [
        {
            "name": "brain_search",
            "description": (
                "Unified search across Chris's brain. Returns ranked atoms with scores. "
                "scope=all|canonical|memory|sessions|working narrows the index slice; "
                "default all. scope=working returns the calling agent's session-scoped "
                "working memory (no durable atoms); requires session_id."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "default": 5, "description": "Max results"},
                    "scope": {
                        "type": "string",
                        "enum": ["all", "canonical", "memory", "sessions", "working"],
                        "default": "all",
                        "description": "Index slice to search",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Required when scope=working — session whose WM is fetched",
                    },
                    "actor": {"type": "string", "description": "Calling agent (audit)"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "brain_remember",
            "description": (
                "Store a fact/preference/decision/lesson in Chris's brain. "
                "durability=durable (default, goes to /memory with supersession), "
                "session (per-session working memory under session_id+actor, no "
                "canonical pollution), or scratch (no persistence — used for "
                "in-turn notes the agent may want to re-read later in the same "
                "session but not save). Pass replaces=[atom_ids] when this "
                "supersedes specific older atoms."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "The memory to store"},
                    "kind": {
                        "type": "string",
                        "enum": ["fact", "preference", "decision", "lesson", "entity", "other"],
                        "default": "fact",
                    },
                    "durability": {
                        "type": "string",
                        "enum": ["durable", "session", "scratch"],
                        "default": "durable",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Required when durability=session or scratch",
                    },
                    "key": {
                        "type": "string",
                        "description": "Optional key for durability=session lookup",
                    },
                    "actor": {"type": "string", "description": "Calling agent"},
                    "replaces": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "atom_ids this fact explicitly supersedes",
                    },
                    "replaces_reason": {"type": "string"},
                },
                "required": ["content"],
            },
        },
        {
            "name": "brain_correct",
            "description": (
                "Record an explicit user correction. Posts the corrected atom AND marks "
                "wrong_atom_ids as superseded — bypasses the cosine-similarity gate."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "correction": {"type": "string"},
                    "wrong_atom_ids": {"type": "array", "items": {"type": "string"}},
                    "category": {"type": "string", "default": "fact"},
                    "reason": {"type": "string"},
                    "actor": {"type": "string"},
                },
                "required": ["correction", "wrong_atom_ids"],
            },
        },
        {
            "name": "brain_think",
            "description": (
                "Ask Chris's brain to reason. mode=think (fast first-person answer), "
                "decide (pick among options), reason (multi-hop synthesis). LLM-backed; "
                "may return a structured timeout hint if the 5s MCP window is too tight."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "mode": {
                        "type": "string",
                        "enum": ["think", "decide", "reason"],
                        "default": "think",
                    },
                    "context": {"type": "string"},
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Required for mode=decide",
                    },
                    "domain": {"type": "string"},
                    "actor": {"type": "string"},
                },
                "required": ["question"],
            },
        },
        {
            "name": "brain_feedback",
            "description": (
                "Record an outcome after a brain-backed task/decision/atom/recall was tested. "
                "Feeds decision_ledger + atom deboost weights so future recall improves."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "target_id": {"type": "string"},
                    "target_type": {
                        "type": "string",
                        "enum": ["task", "decision", "atom", "recall"],
                        "default": "task",
                    },
                    "success": {"type": "boolean"},
                    "notes": {"type": "string"},
                    "actor": {"type": "string"},
                },
                "required": ["target_id", "success"],
            },
        },
    ]


def handle_tools_list(params: dict) -> dict:
    if BRAIN_MCP_PROFILE == "minimal":
        return {"tools": _minimal_tool_defs()}
    return {
        "tools": [
            {
                "name": "brain_recall",
                "description": "Search Chris's knowledge base. Returns ranked results with scores.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "limit": {"type": "integer", "description": "Max results (default 5)", "default": 5},
                        "collection": {
                            "type": "string",
                            "description": "Filter by collection: semantic_memory, canonical, experience, patterns",
                        },
                        "exclude_already_used": {
                            "type": "boolean",
                            "description": (
                                "Drop results that mention tools/services Chris already uses "
                                "(per Neo4j (chris)-[:RELATES_TO {relationship:'uses'}]->(t)). "
                                "Set true for tool-recommendation research so candidates Chris "
                                "already runs don't surface as fresh suggestions. Default false."
                            ),
                            "default": False,
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "brain_store",
                "description": (
                    "Store a memory/fact/preference in the brain. "
                    "If this new fact REPLACES specific older atoms (user said "
                    "'I work 8-6 now (was 8-5)', or you're correcting a wrong "
                    "earlier answer), pass their atom_ids in `replaces` so the "
                    "brain explicitly supersedes them instead of guessing. "
                    "For pure user corrections, prefer brain_correct."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "The memory to store"},
                        "category": {
                            "type": "string",
                            "enum": ["preference", "fact", "decision", "entity", "other"],
                        },
                        "agent": {"type": "string", "description": "Your agent name"},
                        "replaces": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "atom_ids this new fact explicitly supersedes (skip the cosine gate)",
                        },
                        "replaces_reason": {
                            "type": "string",
                            "description": "why these atoms are superseded (e.g., 'user said it changed', 'corrected wrong answer')",
                        },
                    },
                    "required": ["content", "category"],
                },
            },
            {
                "name": "brain_correct",
                "description": (
                    "Use when the user corrects a wrong answer you just gave. "
                    "Recall the wrong atom(s) first via brain_recall, then call "
                    "brain_correct with the correction text + the wrong atom_ids. "
                    "The brain marks the wrong atoms superseded and stores the "
                    "correction as the new truth. Always use this for explicit "
                    "user-correction signals like '아니야', 'that's wrong', 'no, "
                    "actually X' — bypasses the cosine-similarity guess."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "correction": {"type": "string", "description": "The corrected fact (what should be true)"},
                        "wrong_atom_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "atom_ids that were wrong and should be superseded",
                        },
                        "category": {
                            "type": "string",
                            "enum": ["preference", "fact", "decision", "entity", "other"],
                            "description": "Category of the corrected fact",
                        },
                        "agent": {"type": "string", "description": "Your agent name"},
                        "reason": {"type": "string", "description": "user's correction context"},
                    },
                    "required": ["correction", "wrong_atom_ids"],
                },
            },
            {
                "name": "brain_decide",
                "description": "Get a preference-grounded decision recommendation from the brain. Slow path (dispatches LLM, 5-30s). Prefer brain_recall for fast lookups; use brain_decide only when you need a ranked recommendation across options with Chris-preference weighting.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "situation": {"type": "string", "description": "The decision context"},
                        "options": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "description": {"type": "string"},
                                },
                            },
                            "description": "Options to evaluate",
                        },
                        "agent": {"type": "string"},
                        "domain": {"type": "string"},
                        "context": {
                            "type": "string",
                            "description": "Optional extra context that should be included in decision evaluation",
                        },
                    },
                    "required": ["situation", "options"],
                },
            },
            {
                "name": "brain_reason",
                "description": "Deep multi-step reasoning with evidence. Slow path (multi-hop, 10-60s). Use only for genuine synthesis questions — brain_recall + brain_decide are faster for most needs.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "description": "The question to analyze"},
                        "agent": {"type": "string"},
                        "domain": {"type": "string"},
                        "context": {"type": "string"},
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
                        "message_type": {
                            "type": "string",
                            "enum": ["info", "alert", "handoff", "decision"],
                            "default": "info",
                        },
                    },
                    "required": ["from_agent", "to_agent", "content"],
                },
            },
            {
                "name": "brain_changes",
                "description": "Show what changed in the brain's knowledge over a time range. Returns added, changed, and removed memories.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "since": {
                            "type": "string",
                            "description": "Start of range: '7d', 'last week', '2026-04-01'",
                            "default": "7d",
                        },
                        "until": {
                            "type": "string",
                            "description": "End of range (default: now)",
                            "default": "now",
                        },
                    },
                },
            },
            {
                "name": "brain_evolution",
                "description": "Trace how a preference or topic has evolved over time. Shows the chronological timeline of changes.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "topic": {
                            "type": "string",
                            "description": "Topic to trace, e.g. 'frontend framework', 'deployment strategy'",
                        },
                    },
                    "required": ["topic"],
                },
            },
            {
                "name": "brain_procedures",
                "description": "Retrieve learned procedures (step-by-step workflows) from past tasks and shell history.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "task_type": {
                            "type": "string",
                            "description": "Filter by type: deploy, git_workflow, docker_workflow, etc.",
                        },
                        "limit": {"type": "integer", "description": "Max results", "default": 5},
                    },
                },
            },
            {
                "name": "brain_outcome",
                "description": "Record the outcome of a recommendation or action. Feeds the accuracy tracker so the brain learns from mistakes.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string", "description": "ID of the task or recommendation"},
                        "success": {
                            "type": "boolean",
                            "description": "Whether the outcome was correct/successful",
                        },
                        "agent": {"type": "string", "description": "Agent reporting the outcome"},
                        "notes": {"type": "string", "description": "What went right or wrong"},
                    },
                    "required": ["task_id", "success"],
                },
            },
            {
                "name": "brain_wm_set",
                "description": "Set a session working-memory key (per-session scratch buffer). Replaces per-agent SCRATCH.md. Use durable=true to promote to atoms on session end.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "agent": {"type": "string"},
                        "key": {"type": "string"},
                        "value": {"type": "string"},
                        "durable": {"type": "boolean", "default": False},
                    },
                    "required": ["session_id", "agent", "key", "value"],
                },
            },
            {
                "name": "brain_wm_get",
                "description": "Get a session working-memory key. Returns the stored value or an error.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "agent": {"type": "string"},
                        "key": {"type": "string"},
                    },
                    "required": ["session_id", "agent", "key"],
                },
            },
            {
                "name": "brain_wm_list",
                "description": "List all session working-memory keys for a (session_id, agent) pair.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "agent": {"type": "string"},
                    },
                    "required": ["session_id", "agent"],
                },
            },
            {
                "name": "brain_ingest_image",
                "description": "Caption an image via the configured subscription CLI vision backend (codex_cli by default) and index it in brain. Gemini REST is explicit opt-in only via BRAIN_VISION_BACKEND=gemini. Send either a local path or base64-encoded bytes.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Absolute local path to the image file"},
                        "base64_data": {
                            "type": "string",
                            "description": "Alternative: base64-encoded image bytes",
                        },
                        "mime_type": {"type": "string", "default": "image/png"},
                        "prompt": {"type": "string", "description": "Optional: custom captioning prompt"},
                    },
                },
            },
            {
                "name": "brain_search_web",
                "description": "Search the web via the local SearXNG meta-search and record the attempt for brain learning. Returns ranked results with per-domain trust scores. Use this instead of any other web search tool — agent-issued web searches feed back into the brain via /recall/feedback so the system learns which sources are reliable.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "limit": {"type": "integer", "description": "Number of results", "default": 10},
                        "agent": {"type": "string", "description": "Your agent name", "default": "mcp"},
                    },
                    "required": ["query"],
                },
            },
            # 2026-04-16 Tier 3 #8: cognitive verbs for a superhuman brain.
            # The first 11 tools are about storing + retrieving. These three
            # are about knowing what the brain doesn't know, deliberate
            # forgetting, and on-demand consolidation — metacognitive ops.
            {
                "name": "brain_forget",
                "description": "Permanently delete a memory by chroma_id. Use when Chris explicitly asks to forget something, when a superseded memory is confirmed obsolete, or when a stored fact is proven wrong. Irreversible — requires the raw Chroma UUID (get one from brain_recall's `id` field).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "memory_id": {
                            "type": "string",
                            "description": "Raw Chroma UUID from brain_recall result `id`",
                        },
                    },
                    "required": ["memory_id"],
                },
            },
            {
                "name": "brain_consolidate",
                "description": "Trigger on-demand sleep consolidation — runs the same co-activation + tier-promotion job that normally fires nightly. Use after a burst of learning (long session, many new memories) when you want tier/supersession/confidence to settle before the scheduled 3am run. Async dispatch; returns pid immediately.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "brain_tick",
                "description": "Mid-session pulse. Returns brain's current observations (contradictions, coding reverts, stale threads, synthesis patterns) so the calling agent can incorporate brain's state without waiting for the daily digest. Fast: <500ms SQL + no LLM. Call every 5-10 turns or after major actions.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "Max observations to return (default 5)",
                            "default": 5,
                        },
                        "min_severity": {
                            "type": "number",
                            "description": "Filter severity >= this (default 4.0)",
                            "default": 4.0,
                        },
                        "agent": {
                            "type": "string",
                            "description": "Calling agent name (used for audit)",
                        },
                    },
                    "required": [],
                },
            },
            {
                "name": "brain_doubt",
                "description": "Surface what the brain is currently uncertain about: low-confidence atoms (confidence<0.4), unresolved semantic contradictions, and stale canonical notes. Use at the start of a research/decision session to know which beliefs to validate, or when Chris asks 'what are you unsure about?' / '잘 모르겠는 거 있어?'.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "Max items per category", "default": 20},
                    },
                },
            },
        ]
    }


_MINIMAL_TOOL_NAMES = frozenset(
    {"brain_search", "brain_remember", "brain_correct", "brain_think", "brain_feedback"}
)


def handle_tools_call(params: dict) -> dict:
    name = params.get("name", "")
    args = params.get("arguments", {})

    # M7-WS8: resolve the calling agent once and thread it through every tool
    actor = args.get("agent") or args.get("from_agent") or args.get("actor") or "mcp"

    # 2026-05-20 BRAIN_MCP_PROFILE=minimal enforces the single-provider
    # contract at dispatch time, not just tools/list. Agents that peek past
    # the advertised surface (e.g. trying brain_forget) get a structured
    # rejection instead of an unguarded admin call.
    if BRAIN_MCP_PROFILE == "minimal" and name not in _MINIMAL_TOOL_NAMES:
        result = {
            "error": f"Tool '{name}' not available in BRAIN_MCP_PROFILE=minimal. "
            f"Allowed: {sorted(_MINIMAL_TOOL_NAMES)}. Switch profile via env var "
            f"or call /memory + /recall/v2 over HTTP for the full surface."
        }
        text = json.dumps(result, indent=2)
        return {"content": [{"type": "text", "text": text}]}

    if name == "brain_recall":
        q = args["query"]
        n = args.get("limit", 5)
        col = args.get("collection", "")
        exclude_already_used = bool(args.get("exclude_already_used", False))
        # 2026-04-17 MCP timeout fix: OpenClaw's bundle-mcp has a default 5s
        # operation timeout. Previously this path passed expand=true which
        # triggers _hyde.expand_query (Jenna/codex CLI dispatch, 2-3s) +
        # cross-encoder rerank (2-3s) → 5-6s total → timeout. gateway.err.log
        # was full of "-32001 Request timed out" errors for brain_recall.
        # Drop expand=true: bilingual expansion in search_unified already
        # provides KR↔EN coverage without the LLM roundtrip. `expand=true`
        # can still be requested via the explicit expand=true URL param on
        # /recall/v2 when deep expansion is actually needed.
        path = f"/recall/v2?q={urllib.parse.quote(q)}&n={n}"
        if col:
            path += f"&collection={col}"
        if exclude_already_used:
            path += "&exclude_already_used=true"
        result = _brain_request("GET", path, actor=actor)

    elif name == "brain_store":
        # 2026-04-20 MCP timeout cap: POST /memory runs ingest_classifier (LLM
        # call with 1h cache, ~500ms hot / up to 3s cold) + embed (~60ms) +
        # ChromaDB/SQLite writes. Usually <1s but LLM-cold-path can spike past
        # 5s under load. 4s cap returns structured hint instead of -32001.
        timeout_hint = "brain_store may hit ingest_classifier LLM (1-3s cold). Retry, or POST /memory via HTTP directly when the 5s MCP window is insufficient."
        try:
            payload: dict = {
                "content": args["content"],
                "category": args.get("category", "fact"),
                "agent": actor,
                "source": "mcp",
            }
            if args.get("replaces"):
                payload["replaces"] = args["replaces"]
            if args.get("replaces_reason"):
                payload["replaces_reason"] = args["replaces_reason"]
            result = _brain_request(
                "POST",
                "/memory",
                payload,
                actor=actor,
                timeout_s=4,
            )
            result = _normalize_timeout_result(result, timeout_hint)
        except Exception as exc:
            result = _timeout_result(timeout_hint, str(exc))

    elif name == "brain_decide":
        # 2026-04-17 fix: OpenClaw's MCP transport has a 5s operation timeout;
        # /brain/decide dispatches an LLM (5-30s) and would always hit
        # -32001. Return a structured timeout hint instead so the agent gets
        # actionable feedback instead of an MCP protocol error.
        timeout_hint = "brain_decide is LLM-backed (5-30s). Try brain_recall for fast lookups, or call /brain/decide via HTTP directly when the 5s MCP window is insufficient."
        try:
            result = _brain_request(
                "POST",
                "/brain/decide",
                {
                    "situation": args["situation"],
                    "options": args.get("options", []),
                    "agent": actor,
                    "domain": args.get("domain"),
                    "context": args.get("context"),
                },
                actor=actor,
                timeout_s=4,
            )
            result = _normalize_timeout_result(result, timeout_hint)
        except Exception as exc:
            result = _timeout_result(timeout_hint, str(exc))

    elif name == "brain_reason":
        timeout_hint = "brain_reason is multi-hop LLM (10-60s). Use brain_recall + brain_decide, or hit /brain/reason via HTTP for the full run."
        try:
            result = _brain_request(
                "POST",
                "/brain/reason",
                {
                    "question": args["question"],
                    "agent": actor,
                    "domain": args.get("domain"),
                    "context": args.get("context"),
                },
                actor=actor,
                timeout_s=4,
            )
            result = _normalize_timeout_result(result, timeout_hint)
        except Exception as exc:
            result = _timeout_result(timeout_hint, str(exc))

    elif name == "brain_ingest":
        # 2026-04-20 MCP timeout cap: ingest runs LLM classify + embed + store
        # (1-5s typical), blows past the 5s MCP window on spikes. 4s cap returns
        # a structured hint instead of -32001.
        timeout_hint = "brain_ingest is LLM-backed (1-5s). Retry, or POST /brain/ingest via HTTP directly when the 5s MCP window is insufficient."
        try:
            result = _brain_request(
                "POST",
                "/brain/ingest",
                {
                    "content": args["content"],
                    "source": args.get("source", "mcp_ingest"),
                },
                actor=actor,
                timeout_s=4,
            )
            result = _normalize_timeout_result(result, timeout_hint)
        except Exception as exc:
            result = _timeout_result(timeout_hint, str(exc))

    elif name == "brain_focus":
        result = _brain_request(
            "POST",
            "/brain/focus",
            {
                "content": args["content"],
                "category": "focus",
                "agent": actor,
            },
            actor=actor,
        )

    elif name == "brain_message":
        result = _brain_request(
            "POST",
            "/brain/messages",
            {
                "from_agent": args["from_agent"],
                "to_agent": args["to_agent"],
                "content": args["content"],
                "message_type": args.get("message_type", "info"),
                "priority": 5,
            },
            actor=actor,
        )

    elif name == "brain_changes":
        since = urllib.parse.quote(args.get("since", "7d"))
        until = urllib.parse.quote(args.get("until", "now"))
        result = _brain_request("GET", f"/brain/changes?since={since}&until={until}", actor=actor)

    elif name == "brain_evolution":
        topic = urllib.parse.quote(args["topic"])
        result = _brain_request("GET", f"/brain/evolution?topic={topic}", actor=actor)

    elif name == "brain_procedures":
        params = f"limit={args.get('limit', 5)}"
        if args.get("task_type"):
            params += f"&task_type={urllib.parse.quote(args['task_type'])}"
        result = _brain_request("GET", f"/brain/procedures?{params}", actor=actor)

    elif name == "brain_outcome":
        task_or_decision_id = str(args["task_id"])
        success = bool(args["success"])
        notes = args.get("notes", "")
        if task_or_decision_id.startswith("decision_"):
            result = _brain_request(
                "POST",
                "/brain/decisions/" + urllib.parse.quote(task_or_decision_id) + "/outcome",
                {
                    "actual_outcome": notes or ("accepted" if success else "rejected"),
                    "outcome_status": "succeeded" if success else "failed",
                    "review_status": "accepted" if success else "needs_review",
                },
                actor=actor,
            )
        else:
            suffix = "/complete?chris_acked=true" if success else "/reject"
            result = _brain_request(
                "POST",
                "/brain/tasks/" + urllib.parse.quote(task_or_decision_id) + suffix,
                {
                    "result": notes,
                    "agent": actor,
                },
                actor=actor,
            )

    elif name == "brain_wm_set":
        result = _brain_request(
            "POST",
            "/brain/wm",
            {
                "session_id": args.get("session_id", ""),
                "agent": args.get("agent", actor),
                "key": args.get("key", ""),
                "value": args.get("value", ""),
                "durable": bool(args.get("durable", False)),
            },
            actor=actor,
        )

    elif name == "brain_wm_get":
        sid = urllib.parse.quote(args.get("session_id", ""), safe="")
        agt = urllib.parse.quote(args.get("agent", actor), safe="")
        key = urllib.parse.quote(args.get("key", ""), safe="")
        result = _brain_request("GET", f"/brain/wm/{sid}/{agt}/{key}", actor=actor)

    elif name == "brain_wm_list":
        sid = urllib.parse.quote(args.get("session_id", ""), safe="")
        agt = urllib.parse.quote(args.get("agent", actor), safe="")
        result = _brain_request("GET", f"/brain/wm/{sid}/{agt}", actor=actor)

    elif name == "brain_ingest_image":
        # 2026-04-20 MCP timeout cap: vision LLM calls often take 2-10s and
        # can exceed the 5s MCP window. 4s cap returns a structured hint.
        timeout_hint = "brain_ingest_image is vision-LLM-backed (subscription CLI by default, 2-10s). Use POST /brain/ingest/image via HTTP directly for the full run."
        try:
            result = _brain_request(
                "POST",
                "/brain/ingest/image",
                {
                    "path": args.get("path"),
                    "base64_data": args.get("base64_data"),
                    "mime_type": args.get("mime_type", "image/png"),
                    "prompt": args.get("prompt"),
                    "agent": actor,
                },
                actor=actor,
                timeout_s=4,
            )
            result = _normalize_timeout_result(result, timeout_hint)
        except Exception as exc:
            result = _timeout_result(timeout_hint, str(exc))

    elif name == "brain_search_web":
        # 2026-04-20 MCP timeout cap: searxng round-trip + ranking is 2-10s,
        # regularly exceeds 5s MCP window (gateway.err: 2026-04-20T13:19 Ellie
        # -32001). 4s cap returns structured hint instead of protocol error.
        timeout_hint = "brain_search_web is network-backed (searxng, 2-10s). Retry, or POST /web/search via HTTP directly when the 5s MCP window is insufficient."
        try:
            result = _brain_request(
                "POST",
                "/web/search",
                {
                    "query": args["query"],
                    "limit": args.get("limit", 10),
                    "agent": actor,
                },
                actor=actor,
                timeout_s=4,
            )
            result = _normalize_timeout_result(result, timeout_hint)
        except Exception as exc:
            result = _timeout_result(timeout_hint, str(exc))

    # 2026-04-16 Tier 3 #8: cognitive verb handlers
    elif name == "brain_forget":
        mem_id = args.get("memory_id", "")
        if not mem_id:
            result = {"error": "memory_id required"}
        else:
            result = _brain_request(
                "DELETE",
                f"/memory/{urllib.parse.quote(mem_id)}",
                actor=actor,
            )

    elif name == "brain_consolidate":
        # 2026-04-20 MCP timeout cap: full consolidation pass is 10s+, always
        # exceeds 5s MCP window. 4s cap returns structured hint.
        timeout_hint = "brain_consolidate runs a full consolidation pass (10s+). POST /brain/consolidate via HTTP directly for the full run."
        try:
            result = _brain_request("POST", "/brain/consolidate", {}, actor=actor, timeout_s=4)
            result = _normalize_timeout_result(result, timeout_hint)
        except Exception as exc:
            result = _timeout_result(timeout_hint, str(exc))

    elif name == "brain_tick":
        # Mid-session pulse. Calls the existing drives endpoint (no LLM, SQL
        # only) and filters client-side by severity so the response stays
        # small for agents that tick frequently.
        limit = int(args.get("limit", 5))
        min_sev = float(args.get("min_severity", 4.0))
        try:
            raw = _brain_request("GET", "/brain/speak/drives", actor=actor, timeout_s=3)
        except Exception as exc:
            result = {"status": "error", "error": str(exc)[:200]}
        else:
            obs = (raw or {}).get("observations", []) if isinstance(raw, dict) else []
            filtered = [o for o in obs if _safe_float(o.get("severity"), 0.0) >= min_sev]
            filtered.sort(key=lambda o: -_safe_float(o.get("severity"), 0.0))
            result = {
                "count": len(filtered),
                "observations": filtered[:limit],
                "threshold": min_sev,
            }

    elif name == "brain_doubt":
        limit = int(args.get("limit", 20))
        result = _brain_request("GET", f"/brain/doubt?limit={limit}", actor=actor)

    elif name == "brain_correct":
        # Explicit user-correction handler. Posts the correction as a new
        # atom AND marks the named wrong atoms as superseded — bypasses
        # the cosine-similarity gate so paraphrases-of-wrong-answers don't
        # accidentally COEXIST with the corrected version.
        timeout_hint = "brain_correct may hit ingest_classifier LLM (1-3s cold). Retry, or POST /memory via HTTP directly when the 5s MCP window is insufficient."
        try:
            wrong_ids = args.get("wrong_atom_ids") or []
            if not wrong_ids or not args.get("correction"):
                result = {
                    "error": "brain_correct requires both `correction` and `wrong_atom_ids`",
                }
            else:
                payload = {
                    "content": args["correction"],
                    "category": args.get("category", "fact"),
                    "agent": actor,
                    "source": "mcp:brain_correct",
                    "replaces": wrong_ids,
                    "replaces_reason": args.get("reason") or "user-correction via brain_correct",
                }
                result = _brain_request("POST", "/memory", payload, actor=actor, timeout_s=4)
                result = _normalize_timeout_result(result, timeout_hint)
        except Exception as exc:
            result = _timeout_result(timeout_hint, str(exc))

    # 2026-05-20 BRAIN_MCP_PROFILE=minimal: 5-tool single-provider surface.
    # brain_correct above already serves the minimal contract as-is.
    elif name == "brain_search":
        q = args["query"]
        n = int(args.get("limit", 5))
        scope = (args.get("scope") or "all").strip().lower()
        if scope == "working":
            # 2026-05-20 W3.5 round 2: working-memory scope lists the calling
            # agent's session-scoped WM entries. No semantic ranking — WM is
            # exact-key by design; the `query` arg is logged but ignored.
            sid = args.get("session_id")
            if not sid:
                result = {"error": "scope=working requires session_id"}
            else:
                path = (
                    f"/brain/wm/{urllib.parse.quote(sid, safe='')}/"
                    f"{urllib.parse.quote(actor, safe='')}"
                )
                result = _brain_request("GET", path, actor=actor)
        elif scope == "sessions":
            # 2026-05-20 W3: route session-scoped search to the dedicated
            # FTS5 endpoint over raw_events. The route uses filter_actor
            # (not "actor") to avoid collision with the audit ?actor=
            # query param that _brain_request appends automatically.
            path = f"/brain/sessions/search?q={urllib.parse.quote(q)}&n={n}"
            opt_actor = args.get("session_actor") or args.get("filter_actor")
            if opt_actor:
                path += f"&filter_actor={urllib.parse.quote(opt_actor)}"
            opt_session = args.get("session_id")
            if opt_session:
                path += f"&session_id={urllib.parse.quote(opt_session)}"
            opt_source_type = args.get("session_source_type")
            if opt_source_type:
                path += f"&source_type={urllib.parse.quote(opt_source_type)}"
            result = _brain_request("GET", path, actor=actor)
        else:
            path = f"/recall/v2?q={urllib.parse.quote(q)}&n={n}"
            if scope == "canonical":
                path += "&collection=canonical"
            elif scope == "memory":
                path += "&collection=semantic_memory"
            result = _brain_request("GET", path, actor=actor)

    elif name == "brain_remember":
        # 2026-05-20 W3.5 round 2: durability tier routes scratch/session
        # writes to /brain/wm so temporary task state doesn't pollute the
        # durable atom store. durable (default) keeps prior behavior.
        durability = (args.get("durability") or "durable").strip().lower()
        if durability in ("session", "scratch"):
            sid = args.get("session_id")
            if not sid:
                result = {
                    "error": f"brain_remember durability={durability} requires session_id"
                }
            else:
                key = args.get("key") or f"scratch_{int(time.time() * 1000)}"
                # `durable=False` on /brain/wm means session-scoped; scratch
                # writes use the same path but the caller treats the entry
                # as ephemeral (no cron clean-up policy distinction at v1).
                result = _brain_request(
                    "POST",
                    "/brain/wm",
                    {
                        "session_id": sid,
                        "agent": actor,
                        "key": key,
                        "value": args["content"],
                        "durable": False,
                    },
                    actor=actor,
                )
        else:
            timeout_hint = (
                "brain_remember may hit ingest_classifier LLM (1-3s cold). Retry, or POST /memory "
                "via HTTP directly when the 5s MCP window is insufficient."
            )
            try:
                payload = {
                    "content": args["content"],
                    "category": args.get("kind", "fact"),
                    "agent": actor,
                    "source": "mcp:brain_remember",
                }
                if args.get("replaces"):
                    payload["replaces"] = args["replaces"]
                if args.get("replaces_reason"):
                    payload["replaces_reason"] = args["replaces_reason"]
                result = _brain_request("POST", "/memory", payload, actor=actor, timeout_s=4)
                result = _normalize_timeout_result(result, timeout_hint)
            except Exception as exc:
                result = _timeout_result(timeout_hint, str(exc))

    elif name == "brain_think":
        mode = (args.get("mode") or "think").strip().lower()
        if mode == "decide":
            timeout_hint = "brain_think mode=decide is LLM-backed (5-30s). Call /brain/decide via HTTP for the full run."
            endpoint = "/brain/decide"
            payload = {
                "situation": args["question"],
                "options": args.get("options", []),
                "agent": actor,
                "domain": args.get("domain"),
                "context": args.get("context"),
            }
        elif mode == "reason":
            timeout_hint = "brain_think mode=reason is multi-hop LLM (10-60s). Call /brain/reason via HTTP for the full run."
            endpoint = "/brain/reason"
            payload = {
                "question": args["question"],
                "agent": actor,
                "domain": args.get("domain"),
                "context": args.get("context"),
            }
        else:
            timeout_hint = "brain_think mode=think runs Jenna synthesis (5-30s). Call /chris/think via HTTP for the full run."
            endpoint = "/chris/think"
            payload = {
                "question": args["question"],
                "context": args.get("context"),
            }
        try:
            result = _brain_request("POST", endpoint, payload, actor=actor, timeout_s=4)
            result = _normalize_timeout_result(result, timeout_hint)
        except Exception as exc:
            result = _timeout_result(timeout_hint, str(exc))

    elif name == "brain_feedback":
        target_id = str(args.get("target_id", ""))
        target_type = (args.get("target_type") or "task").strip().lower()
        success = bool(args.get("success"))
        notes = args.get("notes", "")
        if not target_id:
            result = {"error": "brain_feedback requires target_id"}
        elif target_type == "decision":
            result = _brain_request(
                "POST",
                "/brain/decisions/" + urllib.parse.quote(target_id) + "/outcome",
                {
                    "actual_outcome": notes or ("accepted" if success else "rejected"),
                    "outcome_status": "succeeded" if success else "failed",
                    "review_status": "accepted" if success else "needs_review",
                },
                actor=actor,
            )
        elif target_type == "recall":
            result = _brain_request(
                "POST",
                "/recall/feedback",
                {
                    "recall_id": target_id,
                    "useful": success,
                    "notes": notes,
                    "agent": actor,
                },
                actor=actor,
            )
        elif target_type == "atom":
            # No dedicated atom-feedback route yet; record as a tagged memory
            # the outcome loop already consumes via action_audit.
            result = _brain_request(
                "POST",
                "/memory",
                {
                    "content": f"feedback on atom {target_id}: {'success' if success else 'failure'} — {notes}",
                    "category": "other",
                    "agent": actor,
                    "source": "mcp:brain_feedback",
                },
                actor=actor,
            )
        else:  # task (default)
            suffix = "/complete?chris_acked=true" if success else "/reject"
            result = _brain_request(
                "POST",
                "/brain/tasks/" + urllib.parse.quote(target_id) + suffix,
                {"result": notes, "agent": actor},
                actor=actor,
            )

    else:
        result = {"error": f"Unknown tool: {name}"}

    text = json.dumps(result, indent=2) if isinstance(result, dict) else str(result)
    return {"content": [{"type": "text", "text": text[:4000]}]}


# MCP stdio transport — read JSON-RPC from stdin, write to stdout.
#
# Self-reap lifecycle (added 2026-04-15 after 35-subprocess leak incident):
#
# The openclaw gateway spawns this script as a stdio MCP bridge and tracks it
# in SessionMcpRuntimeManager. Runtimes are only disposed on explicit session
# reset (get-reply-*.js:2939) — sessions that end normally, or gateway
# restarts that abandon the fd, leave the child running with its stdin pipe
# held open from the parent's side. A blocking `for line in sys.stdin` never
# sees EOF. Children accumulated to 35+ on one machine.
#
# Three belt-and-suspenders checks here, all tunable via env:
#   1. Optional IDLE_TIMEOUT_S — no request for N seconds → exit.
#   2. Optional MAX_LIFETIME_S — hard cap on total uptime.
#   3. Parent-death detection — if our original parent died and we got
#      reparented to launchd (PID 1), we're an orphan. Exit immediately.
#
# 2026-04-24: Codex keeps one MCP subprocess per session and may not respawn it
# after a clean self-reap. Default self-reap to disabled so long coding sessions
# do not hit "Transport closed"; OpenClaw can opt in by setting these env vars.
IDLE_TIMEOUT_S = int(os.environ.get("BRAIN_MCP_IDLE_TIMEOUT_S", "0"))
MAX_LIFETIME_S = int(os.environ.get("BRAIN_MCP_MAX_LIFETIME_S", "0"))
POLL_INTERVAL_S = 30.0  # how often to re-check lifecycle conditions
TRANSPORT_DEBUG = os.environ.get("BRAIN_MCP_TRANSPORT_DEBUG") == "1" or os.environ.get(
    "OMX_MCP_TRANSPORT_DEBUG"
) == "1"
LOG_FILE = os.environ.get(
    "BRAIN_MCP_LOG_FILE",
    str(Path("~/server/brain/logs/brain-mcp-server.log").expanduser()),
)


def _log(msg: str) -> None:
    """Log to stderr + optional file. stdout is reserved for JSON-RPC."""
    line = f"[brain_mcp_server pid={os.getpid()}] {msg}\n"
    try:
        sys.stderr.write(line)
        sys.stderr.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass
    if TRANSPORT_DEBUG:
        try:
            log_path = Path(LOG_FILE)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a") as f:
                f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {line}")
        except OSError as exc:
            try:
                sys.stderr.write(f"[brain_mcp_server pid={os.getpid()}] log file write failed: {exc}\n")
                sys.stderr.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass


def _handle_signal(signum: int, _frame: object) -> None:
    _log(f"signal {signum} received, exiting")
    sys.exit(0)


def _mcp_tool_error(message: str) -> dict:
    """Return a valid MCP tool response instead of killing stdio transport."""
    payload = {"status": "error", "error": message[:400]}
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def _timeout_result(hint: str, error: str) -> dict:
    return {"status": "timeout", "hint": hint, "error": error[:200]}


def _normalize_timeout_result(result: dict | str, hint: str) -> dict | str:
    """Convert urllib timeout payloads into the documented structured shape."""
    if isinstance(result, dict):
        error = str(result.get("error") or "")
        if "timed out" in error.lower() or "timeout" in error.lower():
            return _timeout_result(hint, error)
    return result


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _lifecycle_exit_reason(start: float, last_activity: float, original_parent: int) -> str | None:
    now = time.monotonic()
    if IDLE_TIMEOUT_S > 0 and now - last_activity > IDLE_TIMEOUT_S:
        return f"idle > {IDLE_TIMEOUT_S}s"
    if MAX_LIFETIME_S > 0 and now - start > MAX_LIFETIME_S:
        return f"lifetime > {MAX_LIFETIME_S}s"
    current_parent = os.getppid()
    if current_parent != original_parent and current_parent == 1:
        return "parent died (reparented to init)"
    return None


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGHUP, _handle_signal)

    start = time.monotonic()
    last_activity = start
    original_parent = os.getppid()
    _log(
        "started "
        f"parent={original_parent} profile={BRAIN_MCP_PROFILE} "
        f"idle_timeout={IDLE_TIMEOUT_S}s "
        f"max_lifetime={MAX_LIFETIME_S}s debug={int(TRANSPORT_DEBUG)}"
    )

    # Drop down to BufferedReader: select() only sees fd activity, not Python's
    # TextIOWrapper read-ahead. When Claude Code sends initialize +
    # notifications/initialized + tools/list back-to-back, all three land in
    # one OS read; readline() returns the first, the rest sit in the buffer,
    # and select() then waits the full POLL_INTERVAL_S because the fd looks
    # idle. The 30s stall blew past Claude Code's 30s tools/list timeout, so
    # the response always arrived as "unknown message ID". buffer.peek() lets
    # us notice buffered bytes and drain them without a select round-trip.
    stdin_bin = sys.stdin.buffer

    while True:
        exit_after_response = False
        exit_reason = _lifecycle_exit_reason(start, last_activity, original_parent)
        if exit_reason and exit_reason.startswith("parent died"):
            _log(f"{exit_reason}, exiting")
            return

        # Lifecycle guards normally exit when idle/lifetime expires. If a
        # request is already pending, process exactly one frame and then exit.
        # That avoids Codex/OpenClaw seeing "transport closed" for a request
        # that arrived on the boundary while still preventing long-lived leaks.
        timeout = 0 if exit_reason else POLL_INTERVAL_S

        # Skip select() if BufferedReader already has bytes to drain.
        if stdin_bin.peek(1):
            ready: list = [stdin_bin]
        else:
            try:
                ready, _, _ = select.select([stdin_bin], [], [], timeout)
            except (OSError, ValueError):
                # stdin closed hard — exit cleanly.
                _log("stdin select failed, exiting")
                return
        if exit_reason and not ready:
            _log(f"{exit_reason}, exiting")
            return
        if exit_reason and ready:
            exit_after_response = True
        if not ready:
            continue

        line_bytes = stdin_bin.readline()
        if not line_bytes:  # EOF — parent closed its write end
            _log("stdin EOF, exiting")
            return
        line = line_bytes.decode("utf-8", errors="replace").strip()
        if not line:
            continue

        last_activity = time.monotonic()

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
            try:
                result = handle_tools_call(params)
            except Exception as exc:
                _log(f"tools/call failed: {type(exc).__name__}: {exc}")
                result = _mcp_tool_error(f"{type(exc).__name__}: {exc}")
        elif method == "notifications/initialized":
            continue  # no response needed
        else:
            result = {"error": f"Unknown method: {method}"}

        if msg_id is not None:
            response = {"jsonrpc": "2.0", "id": msg_id, "result": result}
            try:
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()
            except (BrokenPipeError, OSError):
                _log("stdout write failed, exiting")
                return
        if exit_after_response:
            _log(f"{exit_reason} after pending response, exiting")
            return


if __name__ == "__main__":
    main()
