# Brain Evolution Plan

> Written 2026-04-17 in response to Chris's ask: "find all weaknesses + improvements, execute."
> Status: Tier 1 shipped this session. Tier 2 awaiting Chris greenlight.

---

## TL;DR

Most of the infrastructure that addresses my earlier weakness-audit **already exists in the codebase but is disconnected or off-by-default.** The improvements are primarily wiring, not new construction. Tier 1 (shipped today) closes 4 loops; Tier 2 activates 4-6 features that touch the production retrieval path and so need Chris's signoff on latency/cost tradeoffs.

---

## Confirmed weaknesses from last session + research

| # | Weakness | Evidence | Addressed by |
|---|----------|----------|--------------|
| W1 | Extended eval 64% vs stable 96% (literal-wording fails post-consolidation) | CLAUDE.md stable eval + canonical consolidation 262→43 | Self-RAG critic (T2), CRAG (T2), FActScore diagnosis (T3) |
| W2 | No outcome-based eval — brain_outcome data not feeding calibration | `confidence_calibration._collect_pairs` read eval only | **Tier 1 patch — shipped** |
| W3 | Single point of failure — brain.db backup strategy unclear | Runbook silent on atoms-truth backup | Propose separate backup job (T2) |
| W4 | Complexity budget — 68 cron, 12-stage retrieval, hard to maintain | `search_unified.search_all` 670-900 | Unified intent classifier (T2) — prune stages by query type |
| W5 | Self-learning drift — eval auto-grows, LoRA A/B, autonomy proposer, no calibration check | Self-grooming baseline is moving target | Calibration monitor on outcomes (T1 shipped), RAGAS (T2) |
| W6 | "Brain as independent entity" but no pre-action mistake memory | `failure_memory.get_similar_lessons` never called internally | **Tier 1 patch — shipped for task_queue; Tier 2 for autonomy.authorize** |
| W7 | Wants Voyager-style skill library for CC + OpenClaw | No mistake-guard hook in CC, no workflow-memory skill | **Tier 1 — 2 skills shipped** |

---

## What shipped this session (Tier 1)

### 1. `~/.claude/skills/mistake-guard/` — Claude Code skill
**PreToolUse hook** on Bash/Edit/Write. Queries `/recall/v2` filtered for correction/lesson/error/reflection category. If top match score ≥ 0.35 AND text looks mistake-shaped, returns `permissionDecision="ask"` with the lesson text. Fail-open on brain unreachable. 3-second curl timeout.

Files:
- `SKILL.md` — hooks config + methodology
- `bin/check-mistakes.sh` — stdin JSON → /recall/v2 → parsed → warning
- Chmod +x applied; confirmed skill is loaded (appeared in this session's skill list)

### 2. `~/.openclaw/skills/workflow-memory/` — OpenClaw skill
Before task: consult `/brain/procedures?task_type=X`, `/brain/lessons?agent=self`, `/recall/v2?q=<task>`. Honor `avoid` fields strictly. Apply `try_next`. After success: brain's `_extract_procedure` auto-fires. On failure: openclaw_dispatch auto-records lesson. Explicit write path documented for edge cases.

Files:
- `SKILL.md` — 3-step methodology (consult → incorporate → record)
- `_meta.json` — ClawhubRegistry manifest (version 0.1.0)

### 3. Procedures + lessons injection in task dispatch
**`brain_core/task_queue.py:631-645`** — previously only heuristics were injected into the agent prompt. Now also:
- `_get_relevant_procedures(title+desc)` — word-overlap match against top-20 procedures (ranked by success_count DESC). Zero LLM/embed cost.
- `_get_relevant_lessons(title+desc, agent)` — delegates to `failure_memory.get_similar_lessons` which uses Neo4j APOC Jaro-Winkler.

Prompt now has three learning modalities: heuristics (IF/THEN/BECAUSE), procedures (multi-step workflows), lessons (failures + avoid + try_next). Closes the Voyager+Reflexion loop inside OpenClaw.

### 4. Outcomes → confidence_calibration wired
**`brain_core/confidence_calibration.py`** — split `_collect_pairs` into:
- `_collect_pairs_from_eval` (unchanged logic)
- `_collect_pairs_from_outcomes(days_window=90)` — new, pulls `(confidence_was, NOT chris_override)` from autonomy.db `outcomes` table
- `_collect_pairs` now merges both

Smoke test: **58 outcome pairs available**. Since `MIN_SAMPLES=50`, this alone is enough to fit calibration. Previously the Platt fit was silently identity because eval_holdout was empty. **The brain has been calibrating blind until today.**

---

## Tier 2 — proposed, needs Chris greenlight

Each touches the production retrieval path or adds LLM-as-judge cost. Summary table + detail below.

| # | Change | File | Latency | LLM cost | Risk |
|---|--------|------|---------|----------|------|
| T2.1 | Enable Self-RAG critic in /recall/v2 | `server.py:~2260` `self_rag.py` (exists) | +200-400ms p95 | Jenna dispatch per /recall | Medium — can flip env var off |
| T2.2 | CRAG always-on with confidence gate | `search_unified.py` + `crag.py` (exists) | +100-200ms p95 | 0 (no LLM) | Low |
| T2.3 | Pre-action lesson query in autonomy.authorize | `autonomy.py` + `failure_memory.py` | +50-150ms | 0 | Low |
| T2.4 | Unified intent classifier (replace 3 regex) | `search_unified.py:600` + new | -100-200ms p95 | 0 | Medium — reshapes routing |
| T2.5 | Programmatic canonical merge critic | `promote_canonical.py:255` | weekly job only | Jenna per merge | Low — merge becomes safer |
| T2.6 | RAGAS-style monitor (5-10% sampled) | new `ragas_monitor.py` | 0 (async) | Jenna/OpenAI, ~$5-10/mo | Low |
| T2.7 | AWM workflow induction in post_session | `post_session.sh` + distill | 0 (nightly) | Jenna, ~$2-5/mo | Low |
| T2.8 | FActScore weekly on canonical pages | new `factscore_eval.py` | 0 (weekly) | Jenna, ~$3-5/wk | Low — diagnostic only |

### T2.1 — Self-RAG critic in `/recall/v2` (Asai 2023, ~1985 citations)
`brain_core/self_rag.py` already has `critique()`. It's **off by default** (`BRAIN_SELF_RAG_ENABLED` env var) and is not called from `_recall_v2`. Proposal: wire at `server.py:~1930` after stage-2 rerank. Blend critique score with CRAG heuristic (already implemented at `self_rag.py:133`). Expose `critique_score` in response payload.

**Solves W1** (extended eval gap): when consolidated canonical returns low-relevance hits for literal queries, critique catches it → trigger HyDE fallback or surface "low confidence" to caller.

### T2.2 — CRAG always-on (Yan 2024, ~478 citations)
`crag.py` exists, `adaptive_rag.py` exists. Currently MULTI queries only. Proposal: always-on with low-confidence gate. No LLM cost — uses observable statistics (top score, spread, CE scores).

**Solves W1 + W4**: catches consolidation recall failures; replaces several downstream reranking stages when confidence is already high.

### T2.3 — Pre-action lesson query in `autonomy.authorize()` — **DEFERRED 2026-04-17**
**Architectural constraint found during code review**: `autonomy.py:7-11` explicitly declares hot-path target <5ms p99 with "NO synchronous LLM, Neo4j, or vector-store calls." The proposed Neo4j `get_similar_lessons` call is 20-50ms — 4-10x the budget — and `authorize()` is called per task.dispatch, per trigger fire, per self_heal, per slo_monitor. Violating the constraint would regress the p99 of every autonomous action.

T1 prompt injection (at task_queue.py:631) already delivers lesson content to the dispatched agent, which is where action actually happens. The agent can self-gate on the `avoid` field.

**Future option**: async observability — fire `get_similar_lessons` in a background pool post-authorize, log when a match is found but don't gate. Gives Chris data on how often brain authorizes actions with matching avoid-lessons. Implement when there's data showing agents ignore the prompt-injected lessons.

### T2.4 — Unified intent classifier
Three regex classifiers run independently today: `_classify_intent` (search_unified.py:600), `extract_temporal_intent` (temporal_router.py:195), `adaptive_rag.classify` (adaptive_rag.py). No shared taxonomy. Every query hits full fan-out.

Proposal: single `classify(q) -> Intent(kind, complexity, source_weights, skip_stages)`. SIMPLE queries skip cross-encoder rerank + MMR + episodic binding. Saves 100-200ms p95 AND simplifies the 12-stage pipeline.

**Solves W4** directly. Also addresses W1 because literal-wording queries can route to raw_events bypass instead of canonical-first.

### T2.5 — Programmatic canonical merge critic
`promote_canonical.py:254` uses Jaccard > 0.7 title similarity for merge detection. The "critic" is Jenna's prompt instruction to mark `**Conflict:**`. No programmatic specificity-loss check, no contradiction detection.

Proposal: insert `canonical_merge_critic(existing, new)` that (a) checks word-level specificity retention — if merged note has fewer distinct technical tokens than either parent, flag as over-generic; (b) queries atoms for contradictions; (c) returns confidence score. Below threshold → pending queue instead of auto-merge.

**Solves W1** (consolidation specificity loss) at the source.

### T2.6 — RAGAS monitor (Es 2023, ~1289 citations)
Sample 5-10% of `/recall` responses, score faithfulness + answer relevance + context precision via LLM-as-judge. Store in metrics table. Alert on regression.

**Solves W5** (calibration drift + no outcome-based eval). Continuous detection of "we're answering worse this week than last."

### T2.7 — Agent Workflow Memory (Wang 2024)
`post_session.sh` distillation currently extracts text learnings. Proposal: enhance Jenna's prompt to also extract **workflow DAGs** from successful multi-step sessions — materialize as `procedures` rows with `source="awm_session"`. This accelerates the Voyager-style skill library Chris wants.

**Solves W7** deeper (currently success → procedure extraction only fires for task_queue tasks; this extends to general Claude Code sessions).

### T2.8 — FActScore on consolidated canonical (Min 2023, ~869 citations)
Weekly job. Decomposes canonical page X into atomic claims, verifies each against the original 262-fragment source docs. **Directly diagnoses W1** — tells us which specific facts were lost in the 262→43 consolidation, per-page.

---

## Tier 3 — research-tier, deferred

- **Generative Agents reflection synthesis** (Park 2023, ~3003 citations): `brain_reflect.py` currently finds contradictions; extend to periodically synthesize meta-observations. Quality depends heavily on Sage — risk of reflection garbage compounding.
- **RAPTOR tree-organized retrieval** (Sarthi 2024, ~509 citations): rebuild canonical as cluster-summary tree. Preserves literal leaves AND consolidated meaning. **Heavy implementation**; may be overkill vs simpler T2.1/T2.2 fixes.
- **A-MEM self-organizing linking** (Xu 2025, ~50 citations, NeurIPS 2025): make `graph_rebuild_mentions` real-time instead of weekly. Adds write latency — not sure gain justifies it.

---

## Tier 4 — explicit NOT doing (and why)

- **Custom Self-RAG token training.** Resource constraint — LoRA already caused OrbStack pressure (per Ellie's 2026-04-17 incident note). Prompt-based Self-RAG from T2.1 covers 80% of the value.
- **Full RAPTOR rebuild of canonical.** Implementation cost > reward given T2.1+T2.2 already address the gap RAPTOR would solve.
- **Mem0 migration.** Brain already has most of what Mem0 offers (atoms + graph + consolidation + time decay). Their 90% token savings claim is from comparison with naive chat memory, not against tuned RAG systems.
- **Real-time A-MEM linking.** `graph_rebuild_mentions` (Sun 3:30) + `graph_backfill_co_mention` (Sun 3:40) are good enough; real-time adds write latency for marginal recall gain.

---

## Hermes Agent absorption (2026-04-17)

Chris clarified his ask: the self-learning skill is one that **gets created through
self-learning** (Voyager-style auto-materialization), not a skill that enables it.
Hermes Agent (NousResearch, ICLR 2026 Oral) is the current state of the art here.

### Patterns to absorb

| Hermes pattern | Current brain state | Proposed integration |
|----------------|---------------------|----------------------|
| **Auto-materialize SKILL.md files after 5+ tool-call complex task** | `_extract_procedure` writes DB rows only | **T2.10** — when procedure.success_count ≥ 2 AND steps ≥ 3, also write SKILL.md to `~/.claude/skills/auto-<slug>/` + `~/.openclaw/skills/auto-<slug>/`. Brain-sourced content. Regenerate when success_count changes. |
| **agentskills.io open standard format** | No standard format | Use same frontmatter structure Hermes uses; interop-ready. |
| **Skills self-improve during use** (trace analysis when better approach found) | Procedures upsert success_count only, never mutate steps | **T2.11** — when a retrieved procedure is used AND task failed, don't just record lesson; fork procedure variant with the delta. |
| **Periodic nudges** (cron extracts patterns → memory) | `canonical_pipeline` + `brain_reflect` + `sm2_nightly` | Already have. Hermes just calls them "nudges" — rename unnecessary. |
| **FTS5 cross-session recall with LLM summarization** | Semantic-only (Qdrant + canonical) | **T2.9** — add SQLite FTS5 index on `raw_events` table. Wire into `search_unified` fan-out. Directly addresses extended eval 64% literal-wording gap. |
| **Honcho dialectic user modeling** (per-session) | Sage `profile_regen` weekly (batch) | **SKIP** — Sage weekly is sufficient for solo user; Honcho's value is multi-user. Explicit no-go. |
| **DSPy + GEPA prompt/skill evolution** ($2-10/run, API-only, no GPU) | LoRA weekly (weight-level only) | **T3** — Sunday 5:15am cron: `gepa_evolve` top-10 most-used procedures. 5 gates: tests pass, size ≤ 15KB, semantic preservation, human review, cache compat. Defer until T2 stable. |

### T2.9 — FTS5 raw_events index (new, added post-Hermes)
Adds SQLite FTS5 virtual table over `raw_events.title + content`. Incremental index maintained by trigger (or rebuilt weekly). `search_unified._search_fts` runs in parallel fan-out alongside RAG/canonical/obsidian. Low cost, addresses a specific diagnosed gap.

### T2.10 — Auto-skill materialization (NEW, the real Chris ask)
Trigger: `task_queue._extract_procedure` creates procedure with success_count ≥ 2. Post-commit hook (or APScheduler job) reads the procedure, generates SKILL.md + `_meta.json` in both CC and OpenClaw skill dirs. Slug = sanitized task_type. Content template includes steps, preconditions, tools, "Related brain procedures" backlinks. Brain remains source of truth — if the procedure row is deleted or demoted, the skill file is auto-removed or marked archived.

### T2.11 — Skill self-mutation from trace (NEW, from Hermes pattern)
When a procedure is retrieved for a task (by `_get_relevant_procedures`), tag the task's outcome record with `procedure_id`. If that task fails, the lesson gets `procedure_id` too. A weekly job scans procedures with ≥ 3 failures against them in last 30 days and dispatches Sage to propose a revised `steps` list. Applied behind canonical-merge-style critic gate.

---

## Top 12 papers ranked (from research pass)

| Rank | Paper | Year | ~Cites | Addresses | Tier |
|------|-------|------|--------|-----------|------|
| 1 | Reflexion (Shinn) | 2023 | 1520 | W6 | T1 ✓ + T2.3 |
| 2 | Self-RAG (Asai) | 2023 | 1985 | W1, W4 | T2.1 |
| 3 | Voyager (Wang) | 2023 | 1173 | W7 | T1 ✓ |
| 4 | Agent Workflow Memory (Wang) | 2024 | 70 | W7 | T2.7 |
| 5 | CRAG (Yan) | 2024 | 478 | W1, W4 | T2.2 |
| 6 | RAGAS (Es) | 2023 | 1289 | W2, W5 | T2.6 |
| 7 | Generative Agents (Park) | 2023 | 3003 | W6 | T3 |
| 8 | FActScore (Min) | 2023 | 869 | W1, W2 | T2.8 |
| 9 | A-MEM (Xu) | 2025 | 50 | W6 | T4 (not doing) |
| 10 | RAPTOR (Sarthi) | 2024 | 509 | W1 | T4 (not doing) |
| 11 | Self-Refine (Madaan) | 2023 | 2548 | W2, W6 | T2.1 blend |
| 12 | Mem0 (Chhikara) | 2025 | 120 | W4 | T4 (already have) |

## Recommended combos

**Combo A (high-leverage, low-cost)** — Reflexion + AWM + CRAG. Addresses W1, W6, W7. Combines T1 (already shipped) + T2.2 + T2.7. Net: ~3 files changed, ~250 lines, no custom model training.

**Combo B (outcome evaluation)** — RAGAS + FActScore + Self-Refine (mini). Addresses W2, W4, W5. Combined T2.6 + T2.8 + T2.1 blend. Creates the missing calibration monitor for the self-learning loop.

**Combo C (pipeline simplification)** — CRAG gate + unified intent classifier + Generative Agents reflection. Addresses W1, W3, W4, W6. Partially already built.

---

## Resource budget

- **Local compute**: zero new heavy jobs. LoRA weekly stays as-is (Sunday 9:30 am). No new cron in 9-6 work hours.
- **LLM API (OpenAI via OpenClaw)**: RAGAS sampled 5-10% ≈ $5-10/mo. FActScore weekly ≈ $3-5/week. AWM distill ≈ $2-5/mo. Self-RAG per /recall — depends on query volume. Hard cap: alert if monthly OpenAI bill > $50.
- **Latency**: Self-RAG adds 200-400ms p95 to /recall/v2 (recall_v2_p95_ms SLO ≤ 350 — close). Unified classifier OFFSETS by 100-200ms. Net: roughly neutral. Must measure before/after.

---

## Self-learning skill architecture (answer to Chris's direct question)

```
┌─────────────────────┐           ┌─────────────────────┐
│  Claude Code        │           │  Hermes profiles    │
│                     │           │                     │
│  mistake-guard      │           │  workflow-memory    │
│  (PreToolUse hook)  │           │  (methodology)      │
└─────────┬───────────┘           └──────────┬──────────┘
          │                                  │
          │  /recall/v2                      │  /brain/procedures
          │  (filter: category=correction/   │  /brain/lessons
          │   lesson/error)                  │  /recall/v2
          ▼                                  ▼
┌─────────────────────────────────────────────────────┐
│  Brain (127.0.0.1:8791)                             │
│                                                     │
│  READ SIDE:                                         │
│  - atoms (semantic_memory, canonical)               │
│  - Neo4j LESSON nodes                               │
│  - autonomy.db procedures                           │
│                                                     │
│  WRITE SIDE (auto):                                 │
│  - task_queue._extract_procedure (success)          │
│  - openclaw_dispatch.record_failure_lesson (fail)   │
│  - post_session.sh /learn (session distill)         │
│                                                     │
│  WRITE SIDE (inside task_queue.py dispatch, NEW):   │
│  - procedures injected into prompt                  │
│  - lessons injected into prompt                     │
└─────────────────────────────────────────────────────┘
```

**Key design principle**: skills are thin READ adapters over the brain. All authoritative state lives in brain. Recording happens via existing automatic paths (SessionEnd, openclaw_dispatch struggle signals, task_queue post-completion). Skills never duplicate writes — they just consult and cite.

---

## 2026-04-17 Session B — Shipped

**All Tier 1 + Combo A + Hermes absorption + Weak-point reinforcement.**

### Shipped in session B (this update)
- **T2.9 FTS5 raw_events** — `raw_events_fts.py` new module + live triggers on brain.db. 7402 rows indexed, 0.08s initial build. `search_unified._search_fts` now merges Qdrant-synced FTS + live raw_events FTS. Direct attack on extended eval 64% literal-wording gap.
- **W3 brain.db backup** — `cli/backup_brain_db.py` + `ai.brain.backup.plist` daily 3:10am. SQLite online .backup, 14-day retention. 36MB brain.db backed up in 31ms. autonomy.db (745KB) in 1ms. First-run verified.
- **W5 calibration drift alarm** — new SLO `calibration_brier_drift_7d` target 0.05 warning. `confidence_calibration.run()` now stores abs(new_brier - prev_brier) to `brain_config_store` → SLO reads on each check. Prevents silent self-learning drift.
- **W7 CLAUDE.md brain-recall guidance** — ACTION BIAS section updated with "Exception — Brain-first lookup BEFORE action" clause listing when brain_recall is the first tool call (preferences/design/infra/corrected-before topics) vs direct action (trivial/self-contained).
- **Skill staleness + overload handler** — `cleanup_stale_auto_skills()` daily at 4:10am. Safety gate requires `auto_generated: true` frontmatter (human skills protected — auto-updater regression caught and fixed).
- **Proactive research scan** — background agent returned 8 patterns ranked. Top priorities added below.

---

## Proactive research findings (2026-04-17)

Agent research surfaced **Contextual Retrieval (Anthropic)** as the highest-leverage next step — directly solves W1 extended eval gap AND consolidation specificity loss.

### Priority 1 — T2.12 Contextual Retrieval (Anthropic Sep 2024) — **SHIPPED 2026-04-17**
**Technique**: LLM prepends 50-100 token context summary to each chunk before embedding + BM25. Prompt: "Here is the document... here is the chunk... give a short succinct context to situate this chunk."
**Gain**: -35% retrieval failure (embeddings only), -49% (+BM25), -67% (+reranking). Widely adopted (LlamaIndex, AWS integrations).
**Cost**: $1.02/M doc tokens with prompt caching. One-time index-time cost (can run Sunday maintenance). ~4-6h implementation.
**Fixes**: W1 literal-wording (CRITICAL), consolidation specificity loss.
**Why top**: Proven, cheap, pure index-time (no hot-path regression), Anthropic-backed.

**What shipped**:
- `brain_core/contextual_embed.py` — per-document prefix generation via Jenna, re-embed canonical chunks with `passage: <prefix>\n\n<chunk>` semantic input
- `contextual_embed_audit` table in brain.db — reproducibility: doc_path + content_hash + prefix + generated_at
- Incremental: skips docs whose content_hash is unchanged since last audit row (cheap weekly refresh)
- Scheduler job: `contextual_embed_weekly` Sunday 5:00am
- Env flag: `BRAIN_CONTEXTUAL_EMBED_ENABLED=1` in brain-server plist
- CLI: `--dry-run`, `--limit N`, `--force`, `--only <subdir>` for safe rollout
- Per-doc strategy (not per-chunk): ~60 active canonical docs × 1 Jenna call = ~60 calls total instead of ~6K. All chunks from the same parent inherit the prefix. Verified prefix quality: "Canonical workflow decision document, global scope, created 2026-04-09 from distilled memory evidence..."
- Verified write path: 5 initial decisions docs → 16 chunks with `contextualized: true` metadata + `contextual_prefix` populated in the vector store

**Verification target**: measure extended eval 64% → 75%+ after full pass completes. Cross-reference against stable eval (should stay ≥95%).

**Actual measurement (2026-04-17 00:00 UTC)** — honest results:
- **Pre-T2.12 extended** (04-16 03:54): content 73.4% / loose 85.1% / source 6.1% / mrr 0.042
- **Post-T2.12 extended** (04-17 00:00): content **72.3%** / loose **85.0%** / source 5.3% / mrr 0.039 → **noise (-1.1pp)**
- **Stable** (04-16 23:54): content **94.9%** unchanged → **no regression ✓**

Why Anthropic's 35-67% gain did NOT transfer:
- Only 280 chunks contextualized out of 6083 in canonical collection (~4.6% coverage)
- 606-query extended eval mostly hits non-contextualized chunks
- Truncation fix (chunk-first order) happened mid-batch, mixed-state embeddings at eval time
- Consolidated canonical pages already have strong summaries baked in — per-doc prefix may add less marginal signal than on raw document chunks

Decision 2026-04-17: T2.12 infrastructure stays live (weekly incremental picks up canonical changes). Next impact investment moves to T2.13 or T2.14. `contextual_embed.py` infrastructure (audit table, session-mode batch apply, CRON job) remains production-useful for future coverage expansion if decided later.

### Priority 2 — T2.13 Full Adaptive-RAG (NAACL 2024)
**Technique**: Small classifier routes queries to no retrieval / single-hop / iterative. Brain already has the scaffold (`adaptive_rag.py` just enabled). Full version adds the trained classifier.
**Cost**: ~8h. Train on existing 58 outcome pairs + eval set.
**Why**: Completes half-built feature; training data already available.

### Priority 3 — T2.14 Clarification loop
**Technique**: When adaptive_rag confidence < threshold, return "clarification needed" with suggested refinements instead of retrieving.
**Cost**: ~4-6h bolt-on.
**Fixes**: No interactive clarification when confidence low.

### Priority 4 — T2.15 HippoRAG 2 upgrade (ICML 2025)
**Technique**: Query-to-triple replaces NER entity extraction; adds passage-level nodes to Neo4j; recognition memory filter.
**Gain**: +7% F1 associative/multi-hop; +12.5% Recall@5.
**Cost**: ~12-16h. Brain already runs HippoRAG v1 with PPR.
**Fixes**: Deeper attack on W1 via better triple matching.

### Priority 5 — T2.16 Self-RAG on + entity groundedness (replaces ReDeEP)
**Technique**: Turn on existing Self-RAG critic (code exists). Add entity overlap scoring between retrieved docs and generated answer as cheap groundedness signal.
**Cost**: ~4h. Code exists, just disabled.
**Fixes**: Retrieval-to-answer hallucination detection (cheap version).

### Priority 6 — T3 GEPA prompt evolution (standalone, 2025)
**Technique**: Reflective prompt evolution. Reads execution traces, proposes targeted variants. Stanford DSPy team, `pip install gepa`.
**Gain**: +6% avg, +20% best-case over GRPO. 35x fewer rollouts than MIPROv2.
**Cost**: ~6-8h wire-up. Uses existing eval_set (109) + outcome pairs (58).
**Note**: Force multiplier, not direct gap fix. Defer until 2-3 direct fixes stable.

### NOT doing (explicit)
- **ColBERT v2 / late interaction** — Contextual Retrieval solves same gap at 1/10 cost. Revisit only if extended eval < 75% after T2.12.
- **ReDeEP hallucination detector** — requires model-internal attention/FFN inspection. Brain uses Claude/OpenAI APIs; Self-RAG critic covers the need.
- **SleepGate** — targets in-model KV caches, not external memory. Principles already in brain's nightly reflect.

---

## Decisions needed from Chris

For each Tier 2 item, respond **ship** or **skip**:

1. T2.1 Self-RAG critic activation (+200-400ms, needs Jenna per /recall) — ship / skip?
2. T2.2 CRAG always-on (no LLM cost, +100-200ms) — ship / skip?
3. T2.3 Pre-action lesson in autonomy.authorize — ship / skip?
4. T2.4 Unified intent classifier (net neutral latency, medium refactor) — ship / skip?
5. T2.5 Canonical merge critic (weekly job, Jenna per merge) — ship / skip?
6. T2.6 RAGAS monitor (~$10/mo) — ship / skip?
7. T2.7 AWM workflow induction (free, nightly) — ship / skip?
8. T2.8 FActScore weekly on canonical (~$15/mo) — ship / skip?

Or **ship all of Combo A** / **ship all of Combo B** / **ship all of Combo C**.
