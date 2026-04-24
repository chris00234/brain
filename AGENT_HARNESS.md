# Agent Harness — Brain Integration Guide

**Audience**: Claude Code, OpenClaw agents (Jenna / Liz / Ellie / Sage / Market), any
other AI agent that needs to read from, write to, or trigger the brain.

**Last updated**: 2026-04-17 after Tier-1/2/3 + data-integrity + harness passes.

---

## TL;DR

Every agent talks to the brain through **ONE of two paths**:

| Path | Best for | Auth | Examples |
|---|---|---|---|
| **MCP stdio** (`brain_mcp_server.py`) | interactive tool use — Claude Code, OpenClaw sessions | Bearer read from `~/.openclaw/credentials/.personal_webhook_secret` | `brain_recall`, `brain_store`, `brain_doubt` |
| **HTTP API** (`http://127.0.0.1:8791`) | batch jobs, schedulers, non-MCP agents | `Authorization: Bearer $(cat ~/.openclaw/credentials/.personal_webhook_secret)` | `GET /recall/v2`, `POST /memory`, `POST /recall/batch` |

Either path is authoritative — they call the same FastAPI backend.

---

## 1. Connecting (one-time per agent)

### Claude Code
MCP already registered in `~/.claude.json` → `mcpServers.brain`. Nothing to do. Tools appear as `brain_recall`, `brain_store`, etc.

### OpenClaw agents
MCP already registered in `~/.openclaw/openclaw.json` → `mcp.servers.brain`. Nothing to do. Each agent's prompt should reference the `brain_*` tools.

### Any HTTP client
```bash
SECRET=$(cat ~/.openclaw/credentials/.personal_webhook_secret)
curl -H "Authorization: Bearer $SECRET" \
     -H "x-agent: my-agent-name" \
     http://127.0.0.1:8791/agent/heartbeat
```

`x-agent` is propagated into `action_audit.actor` so per-agent usage shows up in `/brain/usage`.

---

## 2. Canonical operation map

| Verb | MCP tool | HTTP route | Notes |
|---|---|---|---|
| Search | `brain_recall` | `GET /recall/v2?q=...` | Primary. Returns ranked results with confidence + pending_contradictions. |
| Search (many) | — | `POST /recall/batch` | Up to 20 queries per call. **Use this instead of looping** for agent batch work. |
| Store fact | `brain_store` | `POST /memory` | One memory. |
| Store many | — | `POST /memory/batch` | Up to 50 memories per call. |
| Delete memory | `brain_forget` | `DELETE /memory/{id}` | Permanent. |
| Consolidate | `brain_consolidate` | `POST /brain/consolidate` | Force sleep-consolidate run. |
| Surface uncertainty | `brain_doubt` | `GET /brain/doubt` | Low-confidence atoms + open contradictions + stale canonical. |
| Streaming recall | — | `GET /recall/stream` | SSE; emits `event: fused` + `event: end`. |
| Decide | `brain_decide` | `POST /brain/decide` | Preference-grounded recommendation. |
| Reason | `brain_reason` | `POST /brain/reason` | Deep multi-hop reasoning. |
| Ingest text/URL | `brain_ingest` | `POST /brain/ingest` | Agent dispatches Sage to extract. |
| Ingest image | `brain_ingest_image` | `POST /brain/ingest/image` | Subscription CLI vision by default (`codex_cli`); Gemini is explicit opt-in fallback only. |
| Web search | `brain_search_web` | `POST /web/search` | SearXNG + per-domain trust scoring. |
| Focus/working mem | `brain_focus`, `brain_wm_*` | `POST /brain/focus`, `POST /brain/wm` | Session-scoped. |
| Feedback | — | `POST /recall/feedback` | Report useful/wrong to train LtR + calibration. |

Full OpenAPI: `GET /openapi.json` or `GET /docs`.

---

## 3. What agents should ALWAYS do

### 3.1 Before stating a fact
Query first. The brain is the source of truth for Chris's infra, preferences, decisions.
```
brain_recall(query="chris preferred embedding model", limit=3)
```
Act on the top result if it's above score 50 and `trust_tier >= 2`.

### 3.2 After learning something
Store it.
```
brain_store(content="Chris switched to X because Y", category="preference", agent="claude")
```
Max 5 stores per session. Only durable facts — skip transient session state.

### 3.3 On user feedback
Report it.
```
POST /recall/feedback {
  "query": "what chris prefers for X",
  "result_id": "<top result id>",
  "result_source": "semantic_memory",
  "useful": true,
  "agent": "claude"
}
```
This feeds the Platt calibration + LoRA training pipelines.

### 3.4 Identify yourself
Always pass `agent` in MCP arg or `x-agent` in HTTP header. Used for:
- `action_audit.actor` (per-agent usage metrics)
- Rate limit keying (per-agent, not per-IP)
- Contradiction source tracking

---

## 4. Key contracts

### 4.1 Recall result shape
```json
{
  "query": "...",
  "results": [
    {
      "id": "<vector store id (UUIDv5 or string)>",
      "path": "<file path or graph:// or raptor:// uri>",
      "title": "...",
      "content": "...",
      "score": 74.1,
      "rerank_score": 148.6,
      "trust_tier": 3,
      "collection": "canonical",
      "source_type": "rag",
      "confidence": 0.87,               // ← metacognitive — use for gating
      "confidence_raw": 0.80,           // ← pre-Platt for comparison
      "pending_contradictions": 1,      // ← >0 means DON'T trust without checking /brain/doubt
      "_debug": {
        "canonical_trust_bonus": 8.0    // ← signals whether canonical override fired
      },
      "provenance": {...}
    }
  ],
  "meta_note": "⚠ Low confidence (0.42) — verify before acting · ⚠ Top result has 1 open contradiction — call brain_doubt for both sides",
  "timing": {"total_ms": 324, "search_ms": 307, ...},
  "latency_ms": 324
}
```

**`meta_note` (2026-04-17)**: proactive metacognitive warning. Null/absent
when the brain is confident. Agents should surface this to the user
verbatim OR act on it (e.g., call `brain_doubt` when it references
contradictions). Triggers: calibrated confidence <0.5, pending
contradictions >0, top-2 scores within 5%, or no high-trust match.
Multiple triggers concat with " · ". No LLM call — heuristic-generated,
<1ms latency.

### 4.2 Score bands (post Tier-1/2 calibration)
- `>= 90` : strong match (canonical or top-rank semantic)
- `60–90` : good match (safe to act on)
- `40–60` : weak — verify before acting
- `< 40`  : likely wrong — treat as not-found

### 4.3 Confidence bands (post-Platt)
- `>= 0.8`: act without further verification
- `0.5–0.8`: mention it but flag uncertainty to the user
- `< 0.5`: surface as "possibly..." and offer to store corrected version

### 4.4 pending_contradictions
If `> 0`, the brain has an open contradiction on this fact. Agents should either:
- Call `brain_doubt` to see both sides, OR
- Tell the user "there's a conflict on this — want me to pick a side?"

---

## 5. Error handling

### 5.1 MCP errors
Wrapped as `{"content": [{"type": "text", "text": "{\"error\": \"...\"}" }]}`. Parse inner JSON.
- `"memory_id required"` → missing arg
- `"Unknown tool: X"` → typo in tool name
- `"HTTP 401:..."` → secret rotation; MCP auto-retries once

### 5.2 HTTP errors
- `400` : malformed payload or missing required field
- `401` : invalid bearer token
- `404` : endpoint or resource doesn't exist (check spelling, check `agent/heartbeat` for capabilities)
- `422` : validation failed (Pydantic rejected shape); response body explains
- `429` : rate-limited (back off + retry; rate is per-agent)
- `503` : brain-server starting or dependency unavailable
- `502` : Qdrant/Ollama/Neo4j unreachable

### 5.3 Brain in degraded state
Check `GET /brain/health`. If `status=degraded`:
- Read responses are probably fine
- Writes may fail silently — verify via subsequent recall
- Don't force-retry against a critical breaker

---

## 6. Rate limits (per-agent)

| Route | Limit |
|---|---|
| `/recall/v2` | 3000/min |
| `/recall/batch` | 300/min (each call = up to 20 queries) |
| `/memory` | 30/min |
| `/memory/batch` | 60/min (each = up to 50) |
| `/brain/ingest` | 10/min (LLM-backed — token-cost guard) |
| `/web/search` | 60/min |

If you're going to exceed these, use the batch endpoints.

---

## 7. When brain should NOT be your source

- **Current time / date / timezone**: use system clocks.
- **Live process state, disk space, running jobs**: use bash/system calls.
- **Real-time external facts** (weather, stock, current events): use `brain_search_web`.
- **Session-ephemeral scratch**: use `brain_wm_*` for that specific session, not `brain_store`.

---

## 8. Testing your integration

```bash
# 1. Heartbeat (no auth)
curl http://127.0.0.1:8791/agent/heartbeat

# 2. Auth sanity
SECRET=$(cat ~/.openclaw/credentials/.personal_webhook_secret)
curl -H "Authorization: Bearer $SECRET" http://127.0.0.1:8791/brain/health

# 3. Recall
curl -H "Authorization: Bearer $SECRET" -H "x-agent: my-test-agent" \
  "http://127.0.0.1:8791/recall/v2?q=brain+architecture&n=3" | jq .results[0]

# 4. Store + verify
curl -H "Authorization: Bearer $SECRET" -H "x-agent: my-test-agent" \
  -X POST -H "Content-Type: application/json" \
  -d '{"content":"test memory from my-test-agent","category":"fact","agent":"my-test-agent","source":"test"}' \
  http://127.0.0.1:8791/memory

# 5. Confirm actor propagation
sqlite3 ~/server/brain/logs/brain.db \
  "SELECT actor, COUNT(*) FROM action_audit WHERE actor LIKE 'my-test-%' GROUP BY actor"
```

---

## 9. Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| 401 on every call | secret file not readable | `chmod 600 ~/.openclaw/credentials/.personal_webhook_secret` |
| Empty results on known facts | brain-server pointing at wrong Qdrant | check `/brain/health` → `services.qdrant` |
| MCP tool not found | stale MCP server process | kill `brain_mcp_server.py` PID; Claude Code respawns it |
| Results missing confidence field | older-than-2026-04-16 brain-server | restart: `launchctl kickstart -k gui/$(id -u)/ai.openclaw.brain-server` |
| High p95 latency | RAPTOR firing on short query | fine — it skips queries with fewer than 5 tokens |
| `action_audit.actor=unknown` | agent didn't pass `x-agent` header | always pass it |

---

## 10. Harness debugging

```bash
# Per-agent usage in last hour
sqlite3 ~/server/brain/logs/brain.db \
  "SELECT actor, tool, COUNT(*) FROM action_audit
   WHERE datetime(created_at) > datetime('now','-1 hour')
   GROUP BY actor, tool ORDER BY 3 DESC"

# SSE live tail (useful for understanding push behavior)
curl -N -H "Authorization: Bearer $SECRET" \
  "http://127.0.0.1:8791/recall/stream?q=brain+architecture"

# Request-ID trace — every response carries X-Request-ID; grep logs
grep "request_id=abc123" ~/server/brain/logs/server.log
```

---

**If the harness is failing**: start with `/agent/heartbeat` (unauthenticated). If that works, auth is the issue. If that fails, brain-server isn't running — `launchctl kickstart -k gui/$(id -u)/ai.openclaw.brain-server`.
