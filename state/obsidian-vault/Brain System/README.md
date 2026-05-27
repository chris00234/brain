# Chris Brain — Personal AI Second Brain System

A self-hosted, privacy-first personal intelligence system that remembers, searches, reasons, decides, and acts. Built on FastAPI + ChromaDB + Neo4j + Ollama, with a multi-agent team (OpenClaw) and Claude Code as the interactive coding partner.

**Not a chatbot. A brain.**

_Last updated: 2026-04-13_

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│                       Brain API (FastAPI :8791)                      │
│                            100+ endpoints                            │
│                                                                      │
│  /recall/v2   /brain/decide    /brain/reason    /brain/reason/multihop│
│  /learn       /brain/autopilot /brain/goals     /brain/tasks         │
│  /memory      /brain/facts     /brain/changes   /brain/evolution     │
│  /brain/audit /brain/procedures/brain/triggers  /brain/timetravel    │
│  /brain/graph /brain/health    /brain/proactive /brain/self-heal     │
│  /chris/think /brain/insights  /brain/lessons   /brain/outcomes      │
├──────────────────────────────────────────────────────────────────────┤
│ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌─────────────┐  │
│ │ Search   │ │ Reasoning│ │ Autonomy │ │ Self-    │ │ Neuromorphic│  │
│ │ Pipeline │ │ Engine   │ │ + Goals  │ │ Learning │ │ Retrieval   │  │
│ └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘ └──────┬──────┘  │
│      │            │            │            │              │         │
│ ┌────▼────────────▼────────────▼────────────▼──────────────▼──────┐  │
│ │ ChromaDB (12 collections, 1024-dim) │ Neo4j (224 ent / 1.3k rel)│  │
│ │ Ollama (multilingual-e5-large)      │ Fact Store (SQLite)       │  │
│ │ FTS Index (SQLite)                  │ Audit Log (SQLite)        │  │
│ │ Embed Cache (SQLite, 99% hit)       │ Cross-Encoder Reranker    │  │
│ └───────────────────────────────────────────────────────────────────┘ │
├──────────────────────────────────────────────────────────────────────┤
│  MCP Server (brain_mcp_server.py) — 11 tools for Claude Code         │
│  Agents: Jenna │ Liz │ Ellie │ Sage │ Market │ Claude Code           │
│  Gateway: OpenClaw (:18789) → OpenAI / Anthropic                     │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Search Pipeline (/recall/v2)

The primary search endpoint. 4-source parallel fan-out with intent-aware trust weighting, followed by a full neuromorphic rerank stack.

```
Query
  → Intent Classification (regex: relational / temporal / preference)
  → Bilingual Expansion (Korean ↔ English)
  → HyDE hypothetical-doc expansion (optional, cached)
  → Parallel fan-out (4 sources, shared ThreadPoolExecutor):
    ├── ChromaDB hybrid (vector + keyword) across 12 collections
    ├── Canonical keyword (file-based Jaccard on canonical/)
    ├── Obsidian vault (ChromaDB "obsidian" collection)
    └── Neo4j graph search (entity-grounded, 2-hop spreading activation)
  → RRF Fusion (theoretical-max normalized, intent-adjusted trust weights)
  → Deduplicate (MD5 + Jaccard >0.8, windowed to 80)
  → Cross-encoder rerank (ms-marco-MiniLM, median-fill, disabled at query-time budget)
  → Salience ranking (importance × recency × bonus)
  → Time decay (category-aware half-lives; valid_to penalty for expired facts)
  → Preference recency boost (30-day half-life)
  → Graph-aware boost (entities with graph connections +3–15%)
  → Episodic binding (co-retrieved episode reinforcement)
  → MMR diversity (lambda 0.7)
  → Source diversity (max 3 per source file)
  → Conflict flagging (cross-collection 30–70% token overlap)
  → Provenance chain (canonical → distilled → raw source IDs)
  → Top-N results with per-stage timing breakdown
```

**Eval (baseline):** 76.1% content hit @5 on 744-query bilingual holdout set.
**Latency (observed production):** p50 ~315 ms, p95 ~525 ms, p99 ~930 ms.
A nightly regression gate (`eval_run`) alerts if `hit_content@5` drops >5 pts from the baseline.

### Embedding Model
`blaifa/multilingual-e5-large-instruct` (1024-dim, asymmetric)
- Documents indexed with `"passage: "` prefix
- Queries embedded with `"query: "` prefix
- 512-token context, 1000-char max chunk
- Shared SQLite embedding cache (99%+ hit rate, 6k+ entries)
- **LoRA fine-tune path** present (`brain_core/lora_embedder.py`, `cli/brain_finetune.py`) — training pairs auto-generated from recall feedback

---

## ChromaDB Collections (12 active)

| Collection | Content | Trust | Decay | Docs |
|---|---|---|---|---|
| `canonical` | Canonical + distilled knowledge notes (highest trust) | 1.0 | None | 2,758 |
| `code` | Indexed source files across `~/server/*` repos | 0.9 | None | 4,525 |
| `knowledge` | Docker/nginx configs, per-agent AGENTS.md + TOOLS.md | 0.9 | None | 309 |
| `experience` | Agent learnings, raw inbox records (browser/shell/git/sessions) | 0.85 | 180d | 3,378 |
| `experience_compressed` | Summarized long-tail experience for recall compression | 0.85 | 180d | 2 |
| `context` | Session memories, working buffers | 0.75 | 30d | 436 |
| `semantic_memory` | Self-learned preferences / facts / decisions / entities | 0.8 | 365d (pref 90d) | 252 |
| `obsidian` | Obsidian vault markdown notes | 0.6 | None | 1,057 |
| `personal` | Apple Notes + iMessage + Calendar + Reminders | 0.85 | 90d | 101 |
| `patterns` | Learned procedures / workflow patterns | 0.8 | None | 19 |
| `semantic_contradictions` | Flagged memory conflicts awaiting review | — | — | 0 |
| `healthcheck_probe` | Synthetic probe documents for liveness | — | — | 0 |

**Total: ~12,837 chunks indexed.**

History: consolidated `notes` / `messages` / `calendar` / `tasks` → single `personal` collection. Added `code` (repo indexer), `patterns` (procedural memory), and `experience_compressed` (long-tail compression).

---

## Neo4j Entity Graph

**224 entities, 1,307 relations**, ~7.7k tracked memories, 1M+ tracked accesses.

| Entity Type | Examples | Memory Class |
|---|---|---|
| service | nginx, docker, chromadb, ghost, brain-server | permanent |
| person | chris cho, jenna, liz, ellie, sage, market | permanent |
| decision | move chromadb out of docker, switch embed model | seasonal |
| preference | conventional commits, strict typescript, react + vite | permanent |
| event | brain audit 2026-04-11, easter 2026-04-04 | ephemeral |
| concept | various extracted entities | ephemeral |

### Graph Features
- **Entity resolution**: alias-based (`resolve_entity()`) + embedding similarity (nightly at 3:05 am)
- **Protected entities**: chris cho, jenna, liz, ellie, sage, market, brain, nginx, docker, chromadb, ollama, neo4j — never auto-merged
- **Type-constrained matching**: prevents agent/pet/person name collisions
- **Hebbian learning**: co-retrieved entities strengthen relationship edges (rate-limited; relation count has grown ~2.4× since initial backfill)
- **Biological consolidation** (nightly 2:50 am): Ebbinghaus decay → synaptic pruning → LTP promotion → cluster detection
- **Edge cascade**: after entity merge, parallel edges consolidated (both inbound + outbound)
- **Spreading activation**: 2-hop weighted traversal for query expansion (0.7× decay per hop)
- **Graph search**: entities returned as 4th retrieval source alongside RAG/canonical/obsidian

### Service Dependency Graph
20+ services with `DEPENDS_ON` and `PROXIES` edges parsed from docker-compose.yml and nginx configs via `pipeline/backfill_services.py`.

---

## Deduplication & Conflict Resolution (7 layers)

| Layer | Detection | Resolution | Location |
|---|---|---|---|
| **Ingest dedup** | SHA256 hash + cross-source Jaccard >0.7 (last 100 records) | Skip duplicate | `ingest/*.py` |
| **Semantic memory dedup** | Content hash + cosine <0.08 + Jaccard >0.5 | Merge (keep longer) | `learn.py` |
| **Contradiction detection** | Same category + cosine >0.85 + Jaccard <0.5 | Auto-resolve if newer + confidence gap >0.2, else flag | `learn.py` |
| **Canonical promotion dedup** | Jaccard >0.7 vs existing canonical | Merge (append sources) | `pipeline/promote_canonical.py` |
| **Search-time dedup** | MD5 first-200-chars + Jaccard >0.8 (windowed to 80) | Skip in results | `search_unified.py` |
| **Entity resolution** | Embedding similarity >0.90 + type constraint | Auto-merge >0.95, review 0.90–0.95 | `pipeline/entity_resolution.py` |
| **Boilerplate filter** | "## Statement Review this proposed" + short JSON frontmatter | Skip at index time | `indexer.py` |

### Contradiction Resolution Actions
`POST /memory/contradictions/{id}/resolve` with action:
- `keep_new` — delete old, mark new as superseding
- `keep_old` — delete new
- `merge` — concatenate content, re-embed, delete duplicate
- `both_true` — keep both, mark reviewed
- `dismiss` — false positive, mark reviewed

---

## Structured Fact Store

SQLite-backed `(entity, attribute, value)` triples with temporal validity and supersession chains.

```
POST /brain/facts
{ "entity":"chris cho","attribute":"location","value":"Irvine, California","confidence":0.95 }
→ { "status":"created","id":"fact_abc123" }
```

- **Dedup**: UNIQUE index on `(entity, attribute, normalized_value)`
- **Supersession**: higher-confidence new value supersedes old (transactional)
- **History**: all versions preserved with `status: active | superseded`
- **Temporal**: `valid_from` / `valid_to` for time-bounded facts

---

## Audit Trail

Every merge, conflict, dedup, fact supersession, and resolution decision is logged to SQLite.

```
GET  /brain/audit?type=merge&since=2026-04-01
GET  /brain/audit/stats
POST /brain/audit/{id}/review
```

Events originate from: contradiction resolution, entity merges, semantic memory dedup, canonical promotion merges, fact store supersession, autopilot decisions.

---

## Self-Learning Pipeline (/learn)

```
Session transcript
  → Extract candidates (regex-scored: preferences, corrections, decisions)
  → Distill via Jenna (structured JSON: content, category, confidence, context_tags)
  → Embed with passage: prefix
  → 2-layer dedup:
    ├── Content hash exact match
    └── Semantic similarity (cosine <0.08 + Jaccard >0.5) → merge or skip
  → Store to semantic_memory collection
  → Auto-resolve clear contradictions (newer + higher confidence → delete old)
  → Flag remaining contradictions for review
  → Extract entities into Neo4j (via Sage, background thread)
  → Audit log: all dedup/merge decisions recorded
```

**Memory categories:** `preference` | `fact` | `decision` | `entity` | `other`

A `SessionEnd` hook (`cli/post_session.sh`) dispatches transcripts to `/learn` automatically — feedback capture is unattended.

---

## Canonical Knowledge Pipeline

```
Data Sources → Ingest Adapters → raw/inbox/*.json
  → batch_distill.py (keyword domain inference)
  → distilled/{domain}/*.md
  → batch_propose.py (merge detection, duplicate suppression)
  → score_proposals.py (numeric scoring)
    ├── score ≥ 75 → auto-promote (with dedup against existing canonical)
    ├── 42 ≤ score < 75 → hold for review
    └── score < 42 → rejected/
  → promote_canonical.py:
    ├── Jaccard >0.7 match found → merge into existing (append sources)
    ├── No match → create new canonical note
    ├── Extract entities into Neo4j
    ├── Extract structured facts into fact store
    └── Audit log: merge decisions recorded
```

Superseded canonical notes get `valid_to` timestamp and 0.3× time_decay penalty in search.

---

## Autonomy & Goals (`/brain/autopilot`, `/brain/goals`, `/brain/tasks`)

The brain can plan, dispatch, and track its own work.

- **Autopilot**: toggleable per-agent; proactive task generation (4× daily)
- **Goal decomposition**: `goal_decompose.py` splits high-level goals into tasks
- **Task queue**: `task_queue.py` with dispatch + process endpoints
- **Action triggers**: `action_triggers.py` fires workflows on state changes
- **Procedural memory**: `brain_core/pipeline/skill_extractor.py` learns reusable workflows from agent + shell history, stored in `patterns` collection, retrieved via `/brain/procedures`
- **Outcome feedback loop**: `/brain/outcomes` records whether recommendations worked — fuels next-iteration ranking and the LoRA training-pair generator
- **Self-healing**: `self_heal.py` watches SLOs, signals degradation via `/brain/self-heal/signal`

---

## Temporal Reasoning (`/brain/changes`, `/brain/evolution`, `/brain/timetravel`)

- **`/brain/changes`**: knowledge diff between two timestamps ("what changed this week?")
- **`/brain/evolution`**: how a single preference/topic evolved over time
- **`/brain/timetravel`**: point-in-time recall ("what did I believe about X on 2026-02-15?")

Implemented in `brain_core/temporal.py` and `temporal_reasoning.py`.

---

## Scheduled Jobs (66 total)

Jobs run inside the brain process via APScheduler. Inspect + trigger via `GET /jobs` / `POST /jobs/{name}`.

### Nightly Pipeline (representative subset)
| Time | Job | Purpose |
|---|---|---|
| 2:00 am | `canonical_pipeline` | Promote inbox → distilled → canonical |
| 2:45 am | `brain_reflect` | Sage finds contradictions in semantic_memory |
| 2:50 am | `graph_consolidation` | Ebbinghaus decay + pruning + LTP promotion |
| 3:05 am | `entity_resolution` | Embedding-based entity merge (auto >0.95, review 0.90–0.95) |
| 3:10 am (Sun) | `stale_cleanup` / `memory_lifecycle` | Weekly orphaned-doc removal |
| 3:15 am | `backup` / `backup_verify` | Chroma + Neo4j backup (independent verify pass) |
| 3:30 am | `eval_run` | Nightly regression gate (alerts >5 pt drop) |
| 4:00 am | `log_rotation` | Truncate logs >512 KB |
| 4:30 am | `memory_consolidation` | Long-tail `experience_compressed` compression |
| 5:00 am (Sun) | `profile_regen` | Sage regenerates Chris profile from canonical |

### Additional job classes
- **Ingest**: `active_contacts_ingest`, `browser_ingest`, `claude_code_sessions_ingest`, `git_activity_ingest`, `gmail_ingest`, `ghost_blog_ingest`, `openclaw_sessions_ingest` (staggered throughout the day; heavy ingest off-hours only)
- **Indexing**: `canonical_index`, `code_index_refresh`, `fts_rebuild`, `chroma_integrity`, `hnsw_adaptive`
- **Memory ops**: `memory_pruning`, `memory_pruning_active`, `memory_leak_detector`, `memory_nudge`, `memory_health_report`, `memory_observability`, `auto_resolve_contradictions`
- **Observability**: `content_quality_slo`, `infra_validation`, `feedback_aggregate`, `llm_usage_purge`, `gap_detection`, `focus_aggregate`, `episode_binder`, `event_compressor`
- **Training**: `embed_finetune` (LoRA pair generation + training prep), `lint_memory`

**Work-hours rule**: no heavy Ollama/ChromaDB jobs between 9 am – 6 pm PST. Reindex runs 2× daily (3 am, 11 pm); personal ingest 3× daily (6 am, 2 pm, 10 pm).

### Native Services (launchd)
| Service | Port | Purpose |
|---|---|---|
| `ai.openclaw.brain-server` | 8791 | FastAPI brain API (supervisor KeepAlive) |
| `ai.openclaw.chromadb-native` | 8000 | Vector database |
| `ai.openclaw.ollama-native` | 11434 | Embedding model (`multilingual-e5-large-instruct`) |
| `ai.brain.neo4j` | 7687 | Entity graph database |
| `ai.hermes.gateway-{profile}` | profile-managed | Hermes profile gateways |
| `ai.brain.qdrant-backup` | — | Independent Qdrant backup (separate failure domain) |
| `ai.openclaw.orbstack-watchdog` | — | Docker auto-recovery |
| `ai.openclaw.watchdog` | — | Gateway watchdog |
| `ai.openclaw.log-rotation` | — | Daily log compression |
| `ai.openclaw.command-center` | — | Top-level launcher / status aggregator |

ChromaDB, Ollama, and Neo4j run **natively** on macOS via launchd, not in Docker. OrbStack's VM disk I/O exceeded macOS's ~2.1 GB/day write limit under heavy embed workloads, so these three were moved out. The `server-net` Docker network still handles inter-container communication for the rest of the homelab.

---

## MCP Integration

The brain exposes itself to Claude Code via a local MCP server (`brain_mcp_server.py`).

**Registration** (in `~/.claude.json`):
```json
{
  "mcpServers": {
    "brain": {
      "command": "/Users/chrischo/server/brain/.venv/bin/python3",
      "args": ["/Users/chrischo/server/brain/brain_mcp_server.py"]
    }
  }
}
```

**11 tools exposed:**

| Tool | Purpose |
|---|---|
| `brain_recall` | Search the knowledge base (query, limit, collection) |
| `brain_store` | Store a memory / fact / preference |
| `brain_decide` | Preference-grounded decision recommendation |
| `brain_reason` | Deep multi-step reasoning with evidence |
| `brain_ingest` | Ingest content into the knowledge base |
| `brain_focus` | Set working context (visible to all agents) |
| `brain_message` | Send a message to another agent |
| `brain_changes` | Diff knowledge over a time range |
| `brain_evolution` | Trace how a preference/topic evolved |
| `brain_procedures` | Retrieve learned procedures/workflows |
| `brain_outcome` | Record outcome of a prior recommendation |

HTTP fallback for scripts / non-MCP contexts:
```bash
SECRET=$(cat ~/.brain/credentials/.personal_webhook_secret)
curl -H "Authorization: Bearer $SECRET" "http://127.0.0.1:8791/recall?q=<query>&n=5"
```

---

## Brain UI (`brain.chrischodev.com`)

**21-page** React 19 + Vite + TypeScript + shadcn/ui SPA, served by nginx with a static build volume-mounted into the container. API routes at `/api/*` are rewritten to `brain-server`.

| Page | Purpose |
|---|---|
| Dashboard | Collection counts, route latencies, cache hit rate, system stats |
| **Brain 3D** | Anatomical 3D brain visualization (GLTF model, per-region glow, neural particles, bloom) |
| **Graph** | 3D force-directed entity graph (224 nodes, auto-centering) |
| Search | Interactive `/recall/v2` with per-stage timing breakdown |
| SearchQuality | Query-level ranking diagnostics and pipeline ablations |
| Memory | Browse / create / edit / delete semantic memories |
| Facts | Browse, filter, create structured fact triples |
| Synthesis | Daily / weekly / monthly narrative views |
| Provenance | Trace memory lineage (canonical → distilled → raw) |
| Autopilot | Toggle switch, task approval queue, goals, working context |
| AgentDashboard | Per-agent status, recent actions, preferences |
| Jobs | Scheduler status, manual trigger, run history |
| EvalHistory | Regression-gate accuracy over time |
| Think | First-person Q&A to "Chris" |
| Messages | Inter-agent messaging |
| Contradictions | Review and resolve conflicting memories |
| **Audit** | Timeline of merge / conflict / dedup / autopilot decisions |
| TimeTravel | Point-in-time knowledge snapshots |
| Profile | Canonical Chris profile (Sage-regenerated weekly) |
| Settings | Token management, feature flags |
| (misc) | Small admin panels exposed as subpaths |

### Brain 3D Visualization
- **Model**: 199 KB anatomical brain GLTF (converted from `.obj` via `obj2gltf v2.0`; 9,206 vertices, cortical folds visible), served at `/models/brain.glb`
- **Regions**: 8 color-mapped lobes (frontal=cyan, parietal=green, temporal=amber, occipital=blue, cerebellum=purple, brainstem=green) — classification done in code (`BrainRegions.ts`), not baked into the model
- **Materials**: translucent `MeshStandardMaterial` + wireframe overlay for a holographic look
- **Effects**: bloom post-processing (intensity 3.0), rim lighting, fog
- **Animation**: per-region breathing pulse, 150 neural particles, query-trace animation
- **Interaction**: click region → slide-over panel with collection details
- **Data**: region glow driven by live `/metrics`, entity count from `/brain/graph/stats`

### Tech Stack
React 19, TypeScript, Vite, Tailwind CSS v4, shadcn/ui, React Three Fiber, drei, postprocessing, react-force-graph-3d, Three.js, Zustand, TanStack React Query, Recharts, Sonner

---

## HTTP Connection Pool

Shared `http_pool.py` used by search and indexer modules.
- Per-thread keep-alive connections via `threading.local()`
- 120-second TTL (shorter than Ollama's 5-min idle unload)
- Auto-reconnect with pool eviction on failure
- 5xx logging for debugging

`http_async.py` exposes an async variant for endpoints that fan out concurrently.

---

## Performance

| Metric | Value |
|---|---|
| `/recall/v2` p50 latency | ~315 ms |
| `/recall/v2` p95 latency | ~525 ms |
| `/recall/v2` p99 latency | ~930 ms |
| Eval baseline (content hit @5) | 76.1% |
| Eval suite size | 744 queries (bilingual holdout) |
| Regression gate threshold | 5 pts |
| Embed cache hit rate | 99%+ |
| Embed cache entries | 6k+ |
| Embedding (cached) | <1 ms |
| Embedding (cold) | ~200 ms |
| ChromaDB disk | ~140 MB |
| Total indexed chunks | 12,837 |
| Neo4j entities | 224 |
| Neo4j relations | 1,307 |
| Tracked memory accesses | 1M+ |
| Brain GLTF model | 199 KB |

---

## Codebase Layout

```
~/server/brain/
├── server.py                    # FastAPI entrypoint (100+ endpoints)
├── brain_mcp_server.py          # Local MCP server (11 tools)
├── DATA_SOURCES.md              # Ingest adapter inventory
├── brain_core/                  # Core modules (~55 files)
│   ├── config.py                # Centralized paths + URLs
│   ├── search_unified.py        # 4-source fan-out (RAG + canonical + obsidian + graph)
│   ├── search.py                # ChromaDB hybrid search
│   ├── indexer.py               # Embedding, chunking, collection management
│   ├── learn.py                 # Self-learning (extract → distill → embed → dedup → resolve)
│   ├── entity_graph.py          # Neo4j wrapper (resolve, alias, graph_search, expand)
│   ├── neo4j_client.py          # Neo4j driver
│   ├── graph_consolidation.py   # Nightly: decay + prune + LTP + cluster
│   ├── spreading_activation.py  # 2-hop weighted graph traversal
│   ├── rerank.py                # Token-overlap + vector boost + source boost
│   ├── cross_encoder_rerank.py  # ms-marco-MiniLM reranker
│   ├── cross_encoder_model.py   # Model loader + batched inference
│   ├── rrf.py                   # Reciprocal Rank Fusion
│   ├── time_decay.py            # Temporal freshness + valid_to expiry
│   ├── temporal.py              # /brain/changes, /brain/evolution
│   ├── temporal_reasoning.py    # /brain/timetravel point-in-time queries
│   ├── hyde.py                  # Hypothetical document expansion
│   ├── embed_cache.py           # SQLite embedding cache
│   ├── lora_embedder.py         # LoRA fine-tune path for embeddings
│   ├── fts_index.py             # SQLite FTS5 keyword index
│   ├── fact_store.py            # Structured (entity, attribute, value) triples
│   ├── audit_log.py             # SQLite audit trail
│   ├── reasoning.py             # /brain/decide, /brain/reason
│   ├── reasoning_loop.py        # Multi-hop reasoning controller
│   ├── proactive.py             # 4× daily proactive insight checks
│   ├── autopilot.py             # Autonomy controller
│   ├── goal_decompose.py        # Goal → task decomposition
│   ├── task_queue.py            # Task dispatch + processing
│   ├── action_triggers.py       # State-change → workflow fires
│   ├── boot_context.py          # Agent startup context assembly
│   ├── scheduler.py             # APScheduler (66 jobs)
│   ├── slo_monitor.py           # Content quality + latency SLO tracking
│   ├── self_heal.py             # Degradation signals and auto-recovery
│   ├── memory_lifecycle.py      # Archive + pre-archival fact extraction
│   ├── memory_operations.py     # CRUD + batch memory ops
│   ├── agent_messenger.py       # Inter-agent message bus
│   ├── agent_preferences.py     # Per-agent preference overlays
│   ├── working_memory.py        # Short-term context buffer
│   ├── failure_memory.py        # Failed-action memory for self-learning
│   ├── feedback_aggregator.py   # Recall feedback → training pairs
│   ├── provenance.py            # Canonical → distilled → raw chain
│   ├── metrics_buffer.py        # In-memory metrics ring
│   ├── http_pool.py             # Shared HTTP keep-alive pool
│   ├── http_async.py            # Async HTTP variant
│   ├── batch_lock.py            # Cross-process batch locking
│   ├── maintenance.py           # Log rotation, integrity, stale cleanup
│   ├── hooks.py                 # Claude Code session hook helpers
│   ├── openclaw_dispatch.py     # Gateway dispatch wrapper
│   ├── safe_state.py            # Crash-safe state persistence
│   ├── schema_versions.py       # Schema migration registry
│   ├── tokenizer.py             # Token counting
│   └── pipeline/                # Pipeline submodule (13 files)
│       ├── episode_binder.py
│       ├── event_compressor.py
│       ├── focus_aggregator.py
│       ├── gap_detector.py
│       ├── hnsw_tuner.py
│       ├── llm_usage_purge.py
│       ├── memory_consolidation.py
│       ├── memory_leak_detector.py
│       ├── memory_nudge.py
│       ├── proactive_linker.py
│       ├── reembed_migrator.py
│       ├── skill_extractor.py
│       └── training_pair_generator.py
├── ingest/                      # 14 data-source adapters
│   ├── personal.py              # Apple Notes / iMessage / Calendar / Reminders
│   ├── gmail.py                 # Gmail (IMAP)
│   ├── browser.py               # Browser history
│   ├── shell_history.py         # Shell history
│   ├── git_activity.py          # Git commits across repos
│   ├── obsidian.py              # Obsidian vault
│   ├── ghost_blog.py            # Ghost blog posts
│   ├── claude_code_sessions.py  # Claude Code session transcripts
│   ├── openclaw_sessions.py     # OpenClaw agent sessions
│   ├── code_repos.py            # Source repo indexer
│   ├── active_contacts.py       # Frequently contacted people
│   ├── screen_time.py           # macOS Screen Time
│   ├── healthcheck.py           # Probes
│   └── run_personal.sh          # Personal-ingest orchestrator
├── synthesis/                   # daily / weekly / monthly + reflect + profile_regen
├── pipeline/                    # Canonical pipeline + backfill scripts
│   ├── pipeline_auto.py
│   ├── promote_canonical.py
│   ├── entity_resolution.py
│   ├── backfill_decisions.py
│   ├── backfill_services.py
│   ├── backfill_preferences.py
│   ├── backfill_calendar.py
│   └── migrate_personal.py
├── cli/                         # CLI tools
│   ├── memory_store.py          # Manual memory write
│   ├── rag_learn.py             # Session → learning store
│   ├── batch_learn.py           # Bulk learning ingest
│   ├── brain_finetune.py        # LoRA embedder training
│   ├── brain_export.py          # Full brain export
│   ├── eval_run / eval_gate / eval_compare / eval_sweep …
│   ├── backup_chroma.py / restore_chroma.py / backup_neo4j.py
│   ├── claude_boot.sh           # UserPromptSubmit hook
│   ├── post_session.sh          # SessionEnd hook (auto /learn dispatch)
│   ├── pre_compact.sh / post_compact.sh / stop_check.sh
│   ├── subagent_log.sh / todowrite_sync.sh
│   ├── server_watchdog.sh
│   └── eval_set.json (+ train/holdout splits)
├── tests/                       # Load test + eval baseline
├── logs/                        # Runtime: embedding_cache.db, audit.db, facts.db, metrics
└── requirements.txt

~/server/brain-ui/               # React 19 SPA (21 pages)

~/server/knowledge/              # Data directory (pure data, no code)
├── canonical/                   # Authoritative truth (~80+ notes)
├── distilled/                   # Summarized narratives (~940+ notes)
├── raw/inbox/                   # Ingestion queue
├── reports/                     # Weekly digests + review queue
└── schemas/                     # JSON validation schemas
```

---

## Agents (OpenClaw)

| Agent | Role |
|---|---|
| **Jenna** | Chief of Staff — orchestration, distillation, learn dispatch |
| **Liz** | Principal Engineer — code changes, architecture |
| **Ellie** | Infra — Docker, nginx, Cloudflare, launchd |
| **Sage** | Research — entity extraction, profile regeneration, reflection |
| **Market** | Growth / Content / Ghost blog |
| **Claude Code** | Interactive coding partner (via MCP) |

Hermes profiles use the Brain HTTP/MCP surface plus BrainMemoryProvider for session memory; legacy OpenClaw transcript distillation remains backlog-only.

---

## License

Personal use. MIT-licensed components where noted. The brain is yours.
