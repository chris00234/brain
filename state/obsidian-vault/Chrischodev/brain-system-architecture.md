# Brain System Architecture

## Overview

Chris's **personal knowledge system** ("the Brain") is a self-learning RAG (Retrieval-Augmented Generation) infrastructure that ingests, indexes, searches, synthesizes, and promotes knowledge from personal data sources. It runs on a **M4 Max Mac** with 36GB RAM.

## System Goals

1. **Total recall** — search across all personal data (notes, messages, calendar, emails, browser history, shell commands, Obsidian vault, agent memories) from a single `/recall` API
2. **Self-learning** — every Claude Code / OpenClaw session automatically distills learnings into durable memory
3. **Canonical knowledge** — automated pipeline promotes raw observations into verified, authoritative facts
4. **Zero-maintenance** — all jobs run on autopilot via APScheduler; watchdogs auto-recover failures

## Three-Folder Architecture

```
~/server/brain/       ← FastAPI backend (code)
~/server/knowledge/   ← Canonical data (facts)
~/server/rag/         ← ChromaDB + Ollama (Docker infra)
```

### 1. Brain Server (`~/server/brain/`)

**FastAPI app on port 8791** — the single API surface for all RAG operations.

| Module | Purpose |
|--------|---------|
| `server.py` | FastAPI entrypoint, routes, middleware, job registry |
| `brain_core/search_unified.py` | Fan-out search across RAG + canonical + Obsidian |
| `brain_core/search.py` | ChromaDB hybrid search (vector + keyword) |
| `brain_core/indexer.py` | Document → embedding → ChromaDB upsert (parallel) |
| `brain_core/learn.py` | Session transcript → memory extraction → contradiction detection |
| `brain_core/boot_context.py` | Generates agent boot context from relevant memories |
| `brain_core/hyde.py` | Hypothetical Document Embedding for query expansion |
| `brain_core/rerank.py` | Cross-encoder reranking of search results |
| `brain_core/rrf.py` | Reciprocal Rank Fusion for multi-source merging |
| `brain_core/time_decay.py` | Temporal scoring — recent results rank higher |
| `brain_core/temporal.py` | Date range parsing → ChromaDB `where` clauses |
| `brain_core/scheduler.py` | APScheduler wrapper — 19 cron jobs |
| `brain_core/openclaw_dispatch.py` | Legacy-named compatibility wrapper for Hermes profile dispatch (Jenna, Sage, Liz) |
| `brain_core/memory_lifecycle.py` | Memory decay, archival, extraction |
| `brain_core/maintenance.py` | Log rotation, ChromaDB integrity checks |
| `brain_core/metrics_buffer.py` | Request latency + dispatch metrics |
| `brain_core/config.py` | Centralized path + URL configuration |
| `brain_core/safe_state.py` | Atomic state file operations with file locking |

### 2. Knowledge Store (`~/server/knowledge/`)

**Pure data** — no code. Pipeline writes here.

```
canonical/     ← Authoritative truth (decisions, infra, projects)
  chris/       ← Profile, weekly arcs, monthly arcs
  decisions/   ← Verified decisions
  infra/       ← Infrastructure facts
  projects/    ← Project state
distilled/     ← Summarized daily narratives
  daily/       ← One file per day (YYYY-MM-DD.md)
raw/inbox/     ← Ingestion queue (JSON records, auto-cleaned >30d)
reports/       ← Weekly digests, review queue, pipeline trace
schemas/       ← JSON schemas for validation
```

### 3. RAG Infrastructure (`~/server/rag/`)

| Container | Image | Resources | Purpose |
|-----------|-------|-----------|---------|
| `chromadb` | chromadb/chroma:1.4.1 | 512MB, 1 CPU | Vector database |
| `ollama` | ollama/ollama:0.20.2 | 2GB, 4 CPU | Embedding only (`nomic-embed-text`) |

**Key config:** `OLLAMA_NUM_PARALLEL=4` — 4 concurrent embedding requests.

## ChromaDB Collections

| Collection | Content | Trust |
|-----------|---------|-------|
| `knowledge` | Docker, nginx, agent configs | High |
| `experience` | Errors, patterns, learnings | High |
| `context` | Session memories, working buffers | Medium |
| `semantic_memory` | Persistent agent memories | Medium |
| `semantic_contradictions` | Flagged conflicts | Low |
| `notes` | Apple Notes | High |
| `messages` | iMessage | Medium |
| `calendar` | Apple Calendar | High |
| `tasks` | Apple Reminders | High |
| `obsidian` | This vault! | Medium |

## API Endpoints

**Base:** `http://127.0.0.1:8791` (public: `brain.chrischodev.com`)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/healthz` | GET | Liveness check |
| `/recall` | GET | Search everything (query, limit, source filter) |
| `/recall/v2` | GET | Enhanced search (HyDE, expand, entity filter) |
| `/memory` | POST | Store a semantic memory |
| `/memory` | GET | List/filter semantic memories |
| `/learn` | POST | Submit transcript for auto-distillation |
| `/boot` | GET | Generate agent boot context |
| `/profile/section/{name}` | GET | Read profile section |
| `/jobs` | GET | List scheduled jobs + status |
| `/jobs/{name}` | POST | Trigger a job manually |
| `/contradictions` | GET | List detected contradictions |
| `/metrics` | GET | Request latency + dispatch stats |

## Ingestion Pipeline

6 data sources, each with its own adapter:

| Adapter | Source | Schedule | Agent |
|---------|--------|----------|-------|
| `personal.py` | Apple Notes, iMessage, Calendar, Reminders | 3x/day (6am, 2pm, 10pm) | — |
| `gmail.py` | Gmail via IMAP | 3x/day | Jenna classifies |
| `browser.py` | Safari/Chrome history | 2x/day | Sage classifies |
| `shell_history.py` | Zsh history | 2x/day | — |
| `obsidian.py` | This Obsidian vault (CouchDB sync) | Hourly | — |
| `ghost_blog.py` | Ghost blog posts | Weekly | — |

**Flow:** Source → Adapter → Embedding (Ollama) → ChromaDB upsert

## Synthesis Pipeline

Agents produce higher-level insights from raw data:

| Job | Agent | Schedule | Output |
|-----|-------|----------|--------|
| Daily synthesis | Jenna | 9pm | `distilled/daily/YYYY-MM-DD.md` + raw facts |
| Daily reflection | Jenna | 10pm | Reflection question for tomorrow |
| Weekly synthesis | Sage | Sunday 4am | `canonical/chris/weekly/YYYY-Www.md` |
| Monthly synthesis | Sage | 1st of month 5am | `canonical/chris/monthly/YYYY-MM.md` |
| Nightly reflect | Sage | 2am | Contradiction/pattern detection |

## Canonical Pipeline

Automated knowledge promotion:

```
raw/inbox/*.json  →  batch_distill  →  distilled/*.md
                  →  batch_propose  →  reports/review-queue/*.md
                  →  score_proposals →  auto-promote (score >= 75)
                                    →  held for review (42-75)
                                    →  auto-reject (< 42)
```

Runs weekly (Sunday 2am). Trace log: `reports/pipeline-trace.jsonl`.

## Performance (Post-Optimization — 2026-04-08)

### Search Latency
- **Ontology graph:** Cached in memory (5-min TTL) — was re-parsed from disk every query
- **Canonical notes:** Cached in memory (2-min TTL) — was full filesystem scan every query
- **Query embeddings:** Parallelized (ThreadPoolExecutor) — was sequential
- **Keyword matching:** Word-boundary instead of substring — eliminates false positives

### Indexing Speed
- **Embedding:** Parallelized with 4 workers (`OLLAMA_NUM_PARALLEL=4`) — was sequential
- **Expected:** ~4-5x faster reindex (200 docs: ~20s → ~4-5s)

### Search Quality
- **Content dedup:** Full MD5 hash instead of 100-char prefix — prevents false dedup
- **Keyword score:** Word-boundary matching — "port" no longer matches "report"

## Reliability Improvements (2026-04-08)

| Fix | Impact |
|-----|--------|
| Gmail state saved AFTER dispatch | Prevents email loss on failed Jenna dispatch |
| Empty embedding guard | Raises RuntimeError instead of silently caching garbage |
| ChromaDB restore rollback | Auto-restores from backup if copytree fails |
| Scheduler graceful shutdown | `wait=True` prevents killing mid-upsert jobs |
| File locking (safe_state.py) | fcntl + atomic rename prevents state corruption |
| Synthesis idempotency | Skips re-run if output exists and is >100 bytes |
| batch_distill collision cap | Max 100 retries, prevents infinite loop |
| Thread-safe caches | `threading.Lock` on ontology + notes caches |
| CORS tightened | Localhost-only origins (was wildcard) |
| Server threadpool | Bumped to 16 workers for concurrent request handling |
| Pipeline trace log | JSONL at `reports/pipeline-trace.jsonl` |
| Zero-output warnings | All adapters warn on stderr when dispatch produces nothing |

## Docker Wrapper (OrbStack Fix)

`~/.local/bin/docker` — lockfile wrapper that serializes all docker CLI calls. OrbStack's Docker API socket deadlocks when 3+ concurrent connections are open (orbstack/orbstack#1842). The wrapper uses atomic `mkdir` + PID file to prevent this.

`orbstack_watchdog.sh` — runs every 5 min via launchd. 10s timeout on `docker info` health check, auto-restarts OrbStack on failure.

## Obsidian LiveSync

- **CouchDB** container on port 5984
- **Nginx proxy** at `couchdb.chrischodev.com` (no rate limiting — LiveSync needs burst traffic)
- **CORS:** `app://obsidian.md`, `capacitor://localhost`, `http://localhost`
- **WebSocket/long-polling:** Nginx configured with `Upgrade` + `Connection: upgrade` + 300s read timeout
- **Database:** `obsidian` (5044 docs, ~15MB)

## Brain UI

`brain.chrischodev.com` — React + Vite + shadcn SPA. 8 pages: Dashboard, Search, Memory, Synthesis, Jobs, Think, Contradictions, Settings. Light mode forced. Deploy: `cd brain-ui && npm run build`.

## Key Design Decisions

1. **Ollama is embedder only** — no synthesis, no reasoning. Mechanical LLM work goes through CLI-first Codex; tool/session-heavy agent work goes through Hermes profiles.
2. **Brain server runs natively** (not in Docker) — avoids container overhead, direct access to ChromaDB/Ollama via localhost HTTP.
3. **Every data source has one owning agent** — Jenna owns email classification, Sage owns browser/research, Liz owns code learnings.
4. **Work-hours rule** — no heavy Ollama/ChromaDB jobs between 9am-6pm PST.
5. **Config module** — all paths centralized in `brain_core/config.py`, overridable via env vars.
