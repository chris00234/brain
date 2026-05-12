# Architecture Audit — 2026-05-12

Comprehensive structural audit run after D1-D10 cognitive architecture pass.
Two parallel subagents (code-explorer + code-reviewer) plus direct data
queries.

## What's healthy

- **Neo4j graph**: 4,983 entities, 47,465 relations, 25,935 tracked memory
  accesses, 72 skills, 25 lessons, 4 agents. Healthy and growing.
- **Qdrant collections**: 9 collections, all non-empty except healthcheck
  probe. canonical=13628, code=19084, distilled=10702, experience=13292.
- **brain.db**: 33 tables, well-organized. raw_events FTS5 working.
  atoms tiered (core=280, episodic=1956, semantic=172, obsolete=42).
- **autonomy.db**: 24 tables, clean. outcomes=65602 with rich provenance.
- **28 SLOs**: 0 breached. Backups within window. WAL checkpoints daily.

## Ontology + metadata audit (2026-05-12)

- **atoms.kind**: 7 distinct values, healthy distribution (fact 804,
  preference 786, decision 549, conjecture 101, correction 74,
  entity 71, other 23). No stray kinds.
- **atoms.tier**: 4 distinct, well-separated (episodic 1956, core 280,
  semantic 172, obsolete 42).
- **provenance_json**: 0 rows with malformed JSON. Schema integrity intact
  across the entire atoms table.
- **Neo4j RELATES_TO edges**: 10 relationship types covering 21,564 edges.
  Distribution skews toward auto-extracted (`co_mention` 12131,
  `co_retrieved` 2269, `related_to` 3561) but rich-semantic types are
  populated too (`uses` 797, `depends_on` 787, `part_of` 735, `manages`
  348, `affects` 232, `created` 144, `prefers` 20). 4.2 edges/entity
  average — solid graph density.
- **Cross-DB entity consistency**: brain.db.entities (5135) and Neo4j
  Entity (4983) differ by 3% — explained by entity_graph.py's eventual-
  consistency model (atoms_store writes immediately to brain.db, Neo4j
  sync runs nightly). No data corruption.

Conclusion: the **data layer is healthy**. All structural improvements
remaining live in the application code (god-modules, routes-vs-services
boundary), not in the data model itself.

## Top 5 architectural problems

### 1. God-modules
| File | Lines | Worst function |
|---|---|---|
| `search_unified.py` | **3123** | `search_all()` lines 1708-3028 = **1320-line function** |
| `brain_loop.py` | **2426** | 60+ defs, sense/decide/act layers mixed |
| `task_queue.py` | **2288** | |
| `indexer.py` | **1849** | |
| `routes/recall.py` | **1567** | `recall_v2()` lines 509-1312 = **803-line route handler** |
| `slos.py` | **1539** | |
| `learn.py` | **1517** | |

Each is a multi-day refactor. `search_unified.search_all` is the highest-
leverage target — splitting into stages (retrieve, rerank, CRAG, blend)
would unblock incremental optimization on each stage.

### 2. Massive duplication of primitives
Independently reimplemented across modules:
- `_now_iso()` — **20+ copies**
- `_ensure_schema()` — **9+ copies**
- `_conn()` / `_connect()` — **16+ copies** with subtly different timeout/WAL settings

**Mitigation shipped 2026-05-12 (commit 9f6f258):** new `brain_core/db.py`
exports `now_iso()`, `open_brain_db()`, `open_autonomy_db()`,
`ensure_schema(conn, key, ddl)`, and `transaction(conn)` contextmanager
wrapping BEGIN IMMEDIATE. `social_model.py` migrated as demo. Other
modules can adopt piecemeal.

### 3. Layer violations
Routes contain raw SQL instead of delegating to service modules:
- `routes/session.py` — 19 raw SQL statements
- `routes/command.py` — 15
- `routes/agency.py` (1013 lines) — 13
- `routes/memory.py` (1459 lines) — 12

Worst: `routes/recall.py recall_v2()` is an 803-line route handler.

### 4. HTTP scattered across brain_core (partially intentional)
13 modules do direct `urllib.request` / `httpx` calls instead of going
through `http_pool.py`. Initial audit flagged this as a violation, but
deeper inspection (2026-05-12) shows most are intentional:

- **slo_monitor** — uses raw urllib because `http_pool` keep-alive would
  distort fresh-TCP latency measurements (the whole point of the probe)
- **telegram_alert** — sends `application/x-www-form-urlencoded`, not JSON;
  `http_pool.http_json()` only does JSON
- **vision_llm** — uploads binary image data; http_pool doesn't support it
- **web_search** — uses httpx (async-capable, needed for parallel queries)
- **reranker_client** — has worker discovery + retry logic specific to the
  reranker process lifecycle

True candidates for migration are narrower: `recall_judge.py`, `self_heal.py`,
`pipeline/memory_nudge.py`, `migrations_brain_db.py`. Each is brain-internal
JSON over local HTTP and would benefit from connection reuse. Defer to a
future sprint with explicit per-module review rather than a blanket migration.

### 5. 141 cron jobs with 2-5am congestion
The 2am-5am window has 40+ heavy jobs competing for SQLite locks,
Qdrant writes, local embedder, and LLM CLI slots. Stagger comments
throughout CRON_MAP.md indicate this is already causing contention.

Functional overlap candidates (verify before merging — most distinctions
are real on closer reading):
- `slo_monitor` (hourly) vs `slos_check` (5-min) — **NOT a duplicate**:
  slo_monitor does ACTIVE PROBING with fresh latency samples;
  slos_check reads accumulated SLO state. Complementary.
- `memory_pruning` + `memory_pruning_active` — 5 min apart, no gate
  (real issue — verify the dry-run actually completes first)
- `proactive_check` (3x/day, Sage) vs `proactive_insights` (daily, system)
  — different agents, different cadences, potentially complementary
- `canonical_staleness_check` partially overlaps `canonical_lint` +
  `stale_current_truth` — verify each detector's claim space

## Smaller issues

### Data structure
- **Duplicate `entities` table** in brain.db (5135 rows, active) and
  autonomy.db (170 rows, stale since 2026-04-27). This is **intentional**:
  brain.db is the source of truth via atoms_store; autonomy.db is a
  Neo4j-fallback maintained by entity_graph.py only when Neo4j is down.
  Neo4j has been up consistently, so the fallback is correctly idle.
  Consider renaming autonomy.db.entities → entities_fallback for clarity.

### Dead code (true 0-importer in production)
- `reranker_worker.py` — **NOT actually orphaned**, runs as standalone
  FastAPI service via `ai.openclaw.brain-reranker.plist`. Audit false
  positive (process-launched, not Python-imported).

### Reduced-usage modules (1-2 refs)
- `spreading_activation.py` (378 lines, 1 ref) — HippoRAG-style PPR
  built but not wired into hot search path
- `late_interaction.py` (194 lines, 2 refs)
- `lora_embedder.py` (187 lines)
- `sparse_tokenizer.py` (109 lines, 3 refs)

These are candidates for either deeper wiring or archival.

### Config sprawl
99 `os.getenv()` calls across 30 files. `brain_loop.py` has 7 inline,
`cross_encoder_model.py` has 14 inline. Should flow through `config.py`.

### Test coverage gaps
No dedicated unit tests for the 7 largest production modules:
- `test_search_unified.py` (only basic exists)
- `test_brain_loop.py`
- `test_task_queue.py`
- `test_learn.py`
- `test_memory_lifecycle.py`
- `test_openclaw_dispatch.py`
- `test_indexer.py`

### routes/__init__.py is empty
28 routers each imported individually in server.py:574-601. Auto-discovery
would reduce the 56-line block to ~3 lines.

### sys.path manipulation
server.py:89 + every route file uses `from config import ...` (bare)
instead of `from brain_core.config import ...`. Breaks pylance/mypy
import resolution.

## D1-D10 code review — 5 bugs fixed in commit 9f6f258

1. `social_model.seed_known_agents` — missing BEGIN IMMEDIATE → race
2. `conjecture_validator._find_supporters` — LIKE patterns with unescaped
   metacharacters (entity '100%' would match everything)
3. `belief_state._compute_per_domain_agency` — connection leak on exception
4. `counterfactual_results` — missing UNIQUE on decision_id → double dispatch
5. `episodic_binding._parse_iso` — missing UTC import → tz comparison bugs

All 14 unit tests passing post-fix.

## Recommended next sprint priorities

### High leverage (single-session)
1. Migrate 5-10 highest-traffic modules to `brain_core/db.py` utilities
2. Add observability endpoint missing pieces — `/brain/atoms/stats`,
   `/brain/qdrant/stats`, parity with new `/brain/graph/stats`
3. Cron audit + consolidation — collapse the duplicate slo_monitor /
   slos_check and memory_pruning patterns

### Multi-day refactors
1. Split `search_unified.search_all` into a 4-stage pipeline
2. Split `recall_v2()` route handler into service layer
3. Auto-discover routers in `routes/__init__.py`

### Operational hygiene
1. Decide on autonomy.db.entities — rename for clarity or remove
2. Move 13 HTTP callers in brain_core/ to http_pool.py
3. Backfill unit tests for top-7 god-modules

---

Audit + fixes shipped in commits 9f6f258 and 8b26543. Original
D1-D10 commits: 2686678, 29d8ac4, 97076a3, 79e3807, 35f9e79, d51b909.
