# Brain — Architecture

A single-user, local-first second brain that combines RAG, episodic + semantic memory, an entity knowledge graph, autonomous learning loops, and a peer-thinking layer. Built for one operator (Chris) on one machine (M4 Max Mac Studio) but architected so the components are independently swappable.

## Component diagram (text)

```
                                ┌───────────────────────┐
                                │   Operator surfaces   │
                                │  Claude Code · Agents │
                                │   Web UI · Telegram   │
                                └───────────┬───────────┘
                                            │
                            ┌───────────────┼─────────────────┐
                            │       MCP / HTTP / Web         │
                            │  brain_mcp_server (12 tools)   │
                            │  FastAPI :8791 (130+ routes)   │
                            └───────────────┬─────────────────┘
                                            │
        ┌───────────────────────────────────┼──────────────────────────────┐
        │                                   │                              │
   ┌────▼─────┐                       ┌────▼────┐                    ┌────▼────┐
   │ Recall   │                       │ Memory  │                    │  Brain  │
   │ (read)   │                       │ (write) │                    │  state  │
   │          │                       │         │                    │         │
   │ /recall  │                       │ /memory │                    │ /brain/*│
   │ /recall  │                       │ /learn  │                    │ /jobs/* │
   │   /v2    │                       │ /memory │                    │ SLOs    │
   │ +CRAG    │                       │  /batch │                    │ Atoms   │
   │ +HyDE    │                       │ atoms   │                    │ Auton.  │
   │ +expand  │                       │ ops     │                    │ Eval    │
   └────┬─────┘                       └────┬────┘                    └────┬────┘
        │                                   │                              │
        └─────────┬─────────────────────────┴──────────────────────────────┘
                  │
        ┌─────────▼──────────────────────────────────────────────────┐
        │                  search_unified.search_all                │
        │  parallel fan-out → RRF fuse → CE rerank → time decay     │
        │  → spreading activation → MMR → triple_link boost          │
        └───┬────────┬──────────┬──────────┬──────────┬──────────────┘
            │        │          │          │          │
       ┌────▼──┐ ┌──▼────┐ ┌───▼────┐ ┌───▼────┐ ┌──▼─────┐
       │Chroma │ │Neo4j  │ │atoms   │ │ FTS    │ │SearXNG │
       │ DB    │ │entity │ │store   │ │ index  │ │ web    │
       │       │ │ graph │ │(brain. │ │(SQLite)│ │ search │
       │ 12856 │ │ (Bolt)│ │ db)    │ │        │ │ M6     │
       │chunks │ │       │ │523+    │ │        │ │        │
       │ 12    │ │       │ │atoms   │ │        │ │        │
       │ collxs│ │       │ │        │ │        │ │        │
       └───┬───┘ └───────┘ └────────┘ └────────┘ └────────┘
           │
       ┌───▼──────────────────────────────┐
       │ Ollama 127.0.0.1:11434           │
       │ multilingual-e5-large-instruct   │
       │ 1024-dim, query/passage prefix   │
       │ Apple Silicon GPU/Neural Engine  │
       └──────────────────────────────────┘

                       ▲
                       │ All LLM calls
                       │
       ┌───────────────┴──────────────────┐
       │     openclaw_dispatch (CB+retry) │
       │  → OpenClaw → OpenAI subscription│
       │       Jenna · Liz · Sage         │
       │       Ellie · Market             │
       └──────────────────────────────────┘

                       ▲
                       │ scheduled jobs
                       │
       ┌───────────────┴──────────────────┐
       │ APScheduler (78 cron jobs)       │
       │ ingest · synthesis · eval ·      │
       │ self-learning · backups ·        │
       │ SLOs · canonical pipeline        │
       └──────────────────────────────────┘
```

## Layers

### 1. Operator surfaces
- **Claude Code** — primary operator. Uses 12 brain_* MCP tools (`~/.claude.json`).
- **OpenClaw agents** (jenna/liz/ellie/sage/market) — same 12 tools via `~/.openclaw/openclaw.json`.
- **Brain UI** — React/Vite SPA at `brain.chrischodev.com`, served via nginx, talks to `/api/*`.
- **Telegram** — Sage and Jenna push proactive insights, alerts, weekly digests.

### 2. API gateway (`server.py`, ~5,000 LOC)
FastAPI on `127.0.0.1:8791`. Bearer-token auth (`~/.openclaw/credentials/.personal_webhook_secret`), per-route slowapi rate limits (M5), per-actor adoption tracking via `action_audit` (M7-WS8), ~130 routes covering recall/memory/jobs/SLOs/triggers/holdout/atoms/breakers/quiet hours/denylist.

### 3. Search pipeline (`brain_core/search_unified.py`)
Parallel fan-out across 6 sources → RRF fuse with trust weights → cross-encoder rerank (BGE-reranker-base) → token-overlap rerank → time decay (category-aware) → preference recency boost → graph entity boost → spreading activation (HippoRAG PPR) → triple_link boost (HippoRAG2, M7-WS3) → MMR diversity → source diversity cap. Optional `?iterative=true` activates CRAG (M9) for low-confidence retry.

### 4. Storage layer (3 native services + SQLite)
- **ChromaDB** native at `127.0.0.1:8000` — 12 collections, 12,856 chunks, 1024-dim embeddings.
- **Ollama** native at `127.0.0.1:11434` — embedder only. `multilingual-e5-large-instruct`. Asymmetric `passage:`/`query:` prefixes. Apple Silicon GPU/NE. Zero LLM duty.
- **Neo4j** native at `127.0.0.1:7687` — entity knowledge graph (atoms ↔ entities ↔ relationships). 512MB heap.
- **SQLite WAL** — `brain.db` (atoms truth layer + action_audit + web_search), `autonomy.db` (eval_proposals, autopilot, breakers, accuracy_tracker), `metrics_history.db`, `audit.db`.

### 5. Atoms truth layer (`brain_core/atoms_store.py`)
523+ canonical atoms with SM-2 spaced repetition (`easiness_factor`, `interval_days`, `next_review_at`, `reinforcement_count`), tier promotion (`episodic → semantic → core → obsolete`), supersession chains (`supersedes` / `superseded_by` / `valid_from` / `valid_until`), per-atom provenance + raw_event lineage. Gated by `BRAIN_ATOMS_ENABLED` for write-side, `BRAIN_ATOMS_READ` for read-side filtering.

### 6. LLM dispatch (`brain_core/openclaw_dispatch.py`)
Every LLM call routes through `openclaw agent --agent <name>`. Persistent circuit breaker (`brain_core/breakers.py`), exponential retry, semantic dispatch cache (opt-in), per-agent usage tracking. Hard rule from CLAUDE.md: no direct OpenAI/Anthropic SDK calls anywhere in brain code.

### 7. Self-learning loop (Phase 7 + C)
1. `/recall/feedback` with `wrong_answer=true, expected="..."` → `eval_proposals` (status=candidate)
2. `eval_holdout_promote` (Sun 8:45) → novelty score → status=pending → `eval_holdout_pending.json`
3. `eval_holdout_audit` (Sun 9:15) → Telegram digest to Jenna
4. Human approves/rejects via `/brain/eval-proposals/{id}/{approve|reject}`
5. Approved items append to `eval_holdout.json`
6. `lora_ab_gate` (Sun 9:30) → LoRA A/B promotion gate
7. `embed_finetune` (when training data exceeds threshold) → new adapter

### 8. Autonomy gate (`brain_core/autonomy.py`)
L0–L3 levels per action_kind. Per-kind override via `POST /brain/autonomy/{kind}`. Quiet hours 23:00–07:00 PT. Soft denylist + DENY_PREFIXES. Persistent breakers (5m / 15m / 1h / 4h backoff). Top kill: `BRAIN_AUTOPILOT_DISABLED=1`.

### 9. SLO + alert loop (`brain_core/slos.py`)
6 SLOs in code: `recall_v2_p95_ms` (≤350 warn), `recall_v2_content_hit_pct` (≥95 critical), `breaker_open_count` (=0 critical), `outbox_pending_count` (≤20 warn), `atoms_write_fail_rate_1h` (≤1% warn), `eval_holdout_growth_weekly` (info). Checked every 5 min. Rate-limited Telegram alerts via jenna-bot.

### 10. Cron infrastructure (`brain_core/scheduler.py` + APScheduler)
78 jobs total. Off-hours pipeline: 02:00 canonical → 02:45 reflect → 03:25 sm2_nightly → 03:30 eval_run → 03:50 eval_run_extended → 04:00 profile_regen → 04:45 autonomy_proposer → 05:30 pdf_ingest → 05:45 image_ingest. Sunday cluster: 04:15 hnsw_tune, 08:45 holdout_promote, 09:15 holdout_audit, 09:30 lora_ab_gate.

## Data flow — typical query

```
operator → MCP brain_recall(q="how do we deploy ghost?")
        → brain_mcp_server proxies to /recall/v2?q=...&actor=claude-code
        → server.py recall_v2 handler
        → search_unified.search_all(q, n=5, sources=[rag,canonical,obsidian])
        → parallel: rag(chroma), canonical(chroma), obsidian(chroma), graph(neo4j),
                    fts(sqlite), graph_prefetch(neo4j)
        → RRF fuse with trust weights {0.9, 1.0, 0.6, 0.5, 0.4, 0.7}
        → atoms tier filter (drop superseded / obsolete)
        → cross-encoder rerank top 20
        → token-overlap rerank
        → triple_link boost (+5pt per HippoRAG2-linked entity)
        → time decay (ghost docs are 30d+, low decay)
        → preference recency boost (none — query is factual)
        → graph entity boost (+15% if entity match in top-k)
        → MMR diversity (skip if top-1 is clear winner)
        → source diversity cap (max 3 per file)
        → return top 5 + telemetry
        → background: insert_action_audit(actor=claude-code, tool=brain_recall)
        → operator gets results in ~250-350ms p50
```

## Key files (one-liner each)

| File | Purpose |
|---|---|
| `server.py` | FastAPI gateway, ~130 routes, lifespan, scheduler boot |
| `brain_mcp_server.py` | 12-tool MCP shim — proxies to HTTP |
| `brain_core/search_unified.py` | The hot path — search_all() pipeline |
| `brain_core/atoms_store.py` | SQLite truth layer + action_audit |
| `brain_core/openclaw_dispatch.py` | All LLM calls go through here |
| `brain_core/scheduler.py` | 78 APScheduler cron jobs |
| `brain_core/slos.py` | 6 SLOs + measurement + alert dispatch |
| `brain_core/breakers.py` | Persistent circuit breakers |
| `brain_core/crag.py` | M9 CRAG iterative retrieval (opt-in) |
| `brain_core/web_search.py` | M6 SearXNG learning loop |
| `brain_core/triple_link.py` | M7-WS3 HippoRAG2 query-triple linking |
| `ingest/personal.py` | Apple Notes / iMessage / Calendar / Reminders |
| `ingest/pdfs.py` | M7-WS2a PDF ingestion via Docling |
| `ingest/images.py` | M7-WS2b image OCR + caption pipeline |
| `cli/eval_compare.py` | Eval harness — baseline vs v2 with toggles |
| `cli/eval_sweep.py` | 11-knob runtime tuning sweep |
| `cli/ralph_m7.py` | Phase M7 9-workstream state machine |
| `brain-ui/` | React/Vite SPA at brain.chrischodev.com |

## Performance contract

- **Stable eval (138 queries)**: ≥94% content_hit floor, currently 98.6%. Verified every 30s by `eval_run` (Sun-Sat 3:30am).
- **Extended eval (606 queries)**: target ≥80% content_hit (Phase M7 goal). Currently 68.2%.
- **/recall/v2 p50 latency**: ≤500ms warm, ≤350ms warn SLO. Currently ~330ms on extended.
- **Per-bearer-token rate limit**: `/recall/v2` 600/min (M7-WS7), `/memory` 30/min, `/learn` 10/min, `/brain/reason/multihop` 10/min, `/brain/ingest` 10/min.
- **Atoms write fail rate**: ≤1% over 1h (SLO). Currently 0%.

## Out-of-band assumptions

- The brain runs on Chris's M4 Max Mac Studio. Single user, single tenant, single token. `~/.openclaw/credentials/.personal_webhook_secret` is the auth root.
- Docker is OrbStack. Brain server itself runs natively (not in Docker) via `~/Library/LaunchAgents/ai.openclaw.brain-server.plist`. ChromaDB, Ollama, Neo4j also native via launchd.
- Cloudflare tunnel exposes `brain.chrischodev.com` for remote access. Bearer auth required.
- Every LLM dispatch uses Chris's existing OpenAI subscription via OpenClaw — no per-call billing surprises.
