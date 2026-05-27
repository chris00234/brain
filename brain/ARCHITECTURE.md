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
       │Qdrant │ │Neo4j  │ │atoms   │ │ FTS    │ │SearXNG │
       │ v1.14 │ │entity │ │store   │ │ index  │ │ web    │
       │       │ │ graph │ │(brain. │ │(SQLite)│ │ search │
       │ 33552 │ │ (Bolt)│ │ db)    │ │        │ │ M6     │
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
                       │ LLM / agent dispatch
                       │
       ┌───────────────┴──────────────────┐
       │ cli_llm fallback chain (CB+retry)│
       │ Codex gpt-5.5 → Spark   │
       │ Hermes profiles for agent work  │
       │ via profile gateway services    │
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
- **Claude Code** — primary operator. Uses 20 brain_* MCP tools (`~/.claude.json`).
- **Hermes profiles** (jenna/liz/ellie/sage/market) — same Brain MCP surface via profile configs under `~/.hermes/profiles/<name>/`.
- **Brain UI** — React/Vite SPA at `brain.chrischodev.com`, served via nginx, talks to `/api/*`.
- **Telegram** — final human-only blocker surface. Proactive insights, alerts, and digests are first routed through the Brain escalation policy and subscription-backed LLM/agent handling; Chris is notified only for missing private/current knowledge, credentials/account access, physical access, irreversible authority, or human-only judgment.

### 2. API gateway (`server.py`, ~5,000 LOC)
FastAPI on `127.0.0.1:8791`. Bearer-token auth (`~/.brain/credentials/.personal_webhook_secret`), per-route slowapi rate limits (M5), per-actor adoption tracking via `action_audit` (M7-WS8), ~130 routes covering recall/memory/jobs/SLOs/triggers/holdout/atoms/breakers/quiet hours/denylist.

### 3. Search pipeline (`brain_core/search_unified.py`)
Parallel fan-out across 6 sources → RRF fuse with trust weights → cross-encoder rerank (BGE-reranker-base) → token-overlap rerank → time decay (category-aware) → preference recency boost → graph entity boost → spreading activation (HippoRAG PPR) → triple_link boost (HippoRAG2, M7-WS3) → MMR diversity → source diversity cap. Optional `?iterative=true` activates CRAG (M9) for low-confidence retry.

Cross-encoder scoring runs in an isolated local worker (`brain_core/reranker_worker.py`, launchd label `ai.brain.reranker`, `127.0.0.1:8792`) when `BRAIN_RERANKER_MODE=worker`. The main API calls it via `brain_core/reranker_client.py` and keeps stage-1 retrieval results unchanged if the worker is unavailable. This prevents Torch/MPS allocator growth from accumulating in the long-running `server.py` process; the worker self-recycles on RSS/request/lifetime limits and launchd restarts it.

`/recall/active` adds a lightweight judgment layer before per-turn hook injection. `brain_core/judgment_layer.py` classifies prompt shape, suppresses proceed-only/generic hook noise, sets semantic score and token budgets, and arbitrates canonical/doorbell/semantic/proactive blocks so the hook injects only evidence that is useful for the current turn. `brain_core/judgment_feedback.py` records those decisions beside `action_audit` and exposes `/brain/judgment-report` plus `/brain/judgment-tuning` for evidence-based policy tuning without adding a new daemon or LLM call.

### 4. Storage layer (3 native services + SQLite)
- **Qdrant** 1.17 native at `127.0.0.1:6333` — 7 collections (13→7 collapse via payload discriminators), ~34K points, 1024-dim e5-large-instruct. int8 scalar quantization, HNSW m=16/ef_construct=128, named vectors `dense`+`contextual`+`raptor` on canonical, `sparse` (BM25 via IDF modifier) on every collection. Built from source; supervised by `ai.brain.qdrant`.
- **Ollama** native at `127.0.0.1:11434` — embedder only. `multilingual-e5-large-instruct`. Asymmetric `passage:`/`query:` prefixes. Apple Silicon GPU/NE. Zero LLM duty.
- **Neo4j** native at `127.0.0.1:7687` — entity knowledge graph (atoms ↔ entities ↔ relationships). 512MB heap.
- **SQLite WAL** — `brain.db` (atoms truth layer + action_audit + web_search), `autonomy.db` (eval_proposals, autopilot, breakers, accuracy_tracker), `metrics_history.db`, `audit.db`.

### 5. Atoms truth layer (`brain_core/atoms_store.py`)
523+ canonical atoms with SM-2 spaced repetition (`easiness_factor`, `interval_days`, `next_review_at`, `reinforcement_count`), tier promotion (`episodic → semantic → core → obsolete`), supersession chains (`supersedes` / `superseded_by` / `valid_from` / `valid_until`), per-atom provenance + raw_event lineage. Gated by `BRAIN_ATOMS_ENABLED` for write-side, `BRAIN_ATOMS_READ` for read-side filtering.

### 6. LLM dispatch (`brain_core/cli_llm.py`, `brain_core/openclaw_dispatch.py`)
Mechanical text LLM calls and autonomous Brain background work route through the subscription Codex CLI via `brain_core/cli_llm.py`: Codex `gpt-5.5` first, then `gpt-5.3-codex-spark`. Tool/session-heavy agent work routes through Hermes profiles via the legacy-named `brain_core/openclaw_dispatch.py` compatibility wrapper. Both paths use breakers/backlog so quota degradation queues catch-up work instead of paging Chris. Hard rule: no direct OpenAI/Anthropic SDK billing and no local generation model duty.
Usage/accounting is exposed through `/brain/usage`, backed by `cli_llm.get_usage_stats`, and should report `source=cli_llm` with `primary_model=gpt-5.5` for mechanical dispatch.

`brain_core/escalation_policy.py` gates all Chris-facing notification paths. The default target is LLM/agent self-handling; Telegram is reserved for blockers the LLM cannot resolve itself: missing private/current knowledge, credentials/account access, physical access, irreversible authority, or human-only judgment. If a subscription LLM review returns `HUMAN_NEEDED: ...`, the original path may notify Chris with that specific blocker; `HANDLEABLE: ...` stays inside Brain.

Vision captioning defaults to the subscription CLI path: `brain_core/vision_llm.py` shells out to `codex exec --image` so image captions stay on Chris's existing GPT/Codex subscription. Gemini REST is retained only as an explicit `BRAIN_VISION_BACKEND=gemini` fallback. Both paths use the shared persistent breaker (`vision.codex_cli` / `vision.gemini`) and mirror calls into `llm_usage.db` for SLO/accounting visibility.

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
27 SLOs in code cover recall latency/quality, breakers, queue backlogs, atom writes, eval drift, backups, logs size, entry-contract drift, Telegram delivery, Hermes profile gateway health, task-dispatch truth, Reflexion lesson coverage, and Brain process RSS. Checked every 5 min. Chris-facing alerts use `brain_core/telegram_alert.py` direct Telegram delivery with backlog replay; deterministic remediation runs first for safe mechanical fixes.

### 10. Cron infrastructure (`brain_core/scheduler.py` + APScheduler)
139 jobs total from `brain_core/job_definitions.py` / generated `CRON_MAP.md`. Off-hours pipeline includes canonicalization, reflection, SM-2 review, eval gates, profile regeneration, autonomy proposals, ingestion, backup/restore checks, CRAG/RAGAS/adversarial/holdout gates, privacy-negative/source-governance audits, UI parity audit, and maintenance. Sunday cluster covers heavier tuning/training/backup/eval work.

## Data flow — typical query

```
operator → MCP brain_recall(q="how do we deploy ghost?")
        → brain_mcp_server proxies to /recall/v2?q=...&actor=claude-code
        → server.py recall_v2 handler
        → search_unified.search_all(q, n=5, sources=[rag,canonical,obsidian])
        → parallel: rag(qdrant), canonical(qdrant), obsidian(qdrant), graph(neo4j),
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
| `brain_mcp_server.py` | 20-tool MCP shim — proxies to HTTP |
| `brain_core/judgment_layer.py` | Deterministic per-turn memory arbitration for `/recall/active` |
| `brain_core/judgment_feedback.py` | Sidecar telemetry for active-recall judgment decisions |
| `brain_core/search_unified.py` | The hot path — search_all() pipeline |
| `brain_core/reranker_worker.py` | Isolated Torch/MPS cross-encoder worker with RSS/request/lifetime recycling |
| `brain_core/reranker_client.py` | Main-server HTTP client for the local reranker worker |
| `brain_core/atoms_store.py` | SQLite truth layer + action_audit |
| `brain_core/cli_llm.py` | CLI-first LLM fallback chain: Codex gpt-5.5 → Spark |
| `brain_core/openclaw_dispatch.py` | Legacy-named Hermes profile dispatch compatibility wrapper for agent-session-heavy work |
| `brain_core/scheduler.py` | 138 APScheduler cron jobs |
| `brain_core/slos.py` | 27 SLOs + measurement + alert dispatch |
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

- **Stable eval (138 queries)**: ≥94% content_hit floor, currently 96.4% content_hit / 83.3% source_hit. Verified daily by `eval_run` (Sun-Sat 3:30am).
- **Extended eval (606 queries)**: target ≥80% content_hit (Phase M7 goal). Currently 75.7% content_hit / 69.5% source_hit.
- **/recall/v2 p50 latency**: ≤500ms warm, ≤350ms warn SLO. Currently ~330ms on extended.
- **Per-bearer-token rate limit**: `/recall/v2` 600/min (M7-WS7), `/memory` 30/min, `/learn` 10/min, `/brain/reason/multihop` 10/min, `/brain/ingest` 10/min.
- **Atoms write fail rate**: ≤1% over 1h (SLO). Currently 0%.

## Out-of-band assumptions

- The brain runs on Chris's M4 Max Mac Studio. Single user, single tenant, single token. `~/.brain/credentials/.personal_webhook_secret` is the auth root.
- All storage backends native via launchd: `ai.brain.qdrant` (source-built v1.17 binary at `~/.local/bin/qdrant`), `ai.brain.ollama`, `ai.brain.neo4j`. Brain server itself native via `ai.brain.server.plist`. OrbStack is used only for ancillary services (not brain-critical).
- Cloudflare tunnel exposes `brain.chrischodev.com` for remote access. Bearer auth required.
- Text LLM dispatch uses Chris's existing subscription through CLI-first routing (`codex exec` gpt-5.5 primary, Spark fallback); Hermes profiles handle tool/session-heavy agent dispatch. Vision captioning defaults to Codex subscription CLI; Gemini REST is explicit opt-in only.
