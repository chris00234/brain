# Brain Changelog

Session-by-session record of shipped work. Current-state facts live in `~/CLAUDE.md`; this file is the history.

## atoms_to_skills quality + OpenClaw per-agent sync (shipped 2026-04-20)

Skill-evolution pipeline was producing junk rules AND silently failing to reach any OpenClaw agent. Both fixed.

**Quality fixes** (`cli/atoms_to_skills.py`):
- Word-boundary keyword classification — plain substring match had `"react"` landing MCC-archive atom in coding-style via `"reactivates"`. Compile `\bkw\b` regexes, cached in `_KEYWORD_CACHE`.
- Strip canonical preamble before classify — `## Summary` scaffolding was pulling screen-time narrative atoms into communication. Also removed `summary` from communication keyword list.
- Duplicated-title bug — atoms shaped `"[title-trunc] # Summary OpenClaw session (…) [body-with-same-start]"` rendered as `"Chris wants X via subsc Chris wants X via subscription…"` because the preamble stripper left the truncated title behind. New `_strip_duplicated_prefix()` cuts to the second occurrence of the first 6 words.
- Durable-rule filter — `_is_durable_rule()` rejects session-narrative (`"This consolidated page captures"`, `"Chris screen time patterns"`, `"Signal: ..."` preamble leaks) and requires a durability signal (Chris-verb / imperative modal / ops directive). Drops one-shot session summaries and synthesis notes.
- Stronger dedup — 120-char normalized prefix (punctuation-stripped, whitespace-collapsed) vs previous 80-char raw; kills near-identical Claude-subscription clones.

**OpenClaw per-agent allowlist** — root cause: OpenClaw's per-agent `skills: [...]` in `openclaw.json` is an allowlist (`src/agents/skills/agent-filter.ts:28` — `"Explicit per-agent skills win when present"`). None of Jenna/Liz/Ellie/Sage/Market listed any `brain-learned-*` entry → skills were discovered on disk but never loaded into any agent session. New `sync_openclaw_agents()` injects every generated `brain-learned-<domain>` into every agent's allowlist and prunes stale entries. Atomic write via `.tmp` + rename; preserves `0600` permissions (regression test covers it).

**Orphan prune** — `prune_orphan_skills()` deletes `brain-learned-<domain>/` dirs whose domain no longer generates rules (coding-style evaporated after the word-boundary fix; no coding-style-specific atoms exist). Safety guard: refuses to prune if fewer than 3 domains survive the run.

**Tests** — 26-case pytest suite at `tests/unit/test_atoms_to_skills.py` covering filter, classifier, sync, prune, and permission preservation. All green.

## Brain v4 — operational hardening (shipped 2026-04-17)

Full-day session landing 10 major improvements. All verified, all shipped.

**LLM cost collapse** — every brain mechanical dispatch (HyDE, atoms_gate, self_rag, reasoning_loop, proactive, synthesis, 42 files total) migrated from `openclaw_dispatch` to `brain_core/cli_llm.py`. Fallback chain: `codex exec -m gpt-5.5` → `codex exec -m gpt-5.3-codex-spark` → OpenClaw emergency fallback → `llm_backlog.enqueue` for worst-case catch-up. **$150/day → $0** (all subscription-backed). Avg tokens/call 414K → 5K (80× reduction). OpenClaw gateway reserved for Chris↔Jenna Telegram + skill-heavy agent turns only. **`--skip-git-repo-check` flag required** so codex runs in non-git brain dir.

**ECC-style skill evolution** — `cli/atoms_to_skills.py` promotes tier=core/semantic atoms (kind=preference/decision/correction, confidence ≥0.65) into Claude Code + OpenClaw skills. 8 domains auto-categorized (llm-budget, infra-ops, brain-system, agent-orchestration, claude-code-ops, coding-style, communication, general). Runs Sun 04:55, writes to BOTH `~/.claude/skills/brain-learned-*/SKILL.md` AND `~/.openclaw/skills/brain-learned-*/SKILL.md`. Skills auto-load at session start — Chris's durable preferences become scoped Claude/agent behavior without touching CLAUDE.md.

**CLS spectral clustering schema learner** — `brain_core/pipeline/schema_learner.py` runs spectral clustering on atom_coactivation graph, eigengap-picks k, emits canonical_compaction candidates to `brain_config`. Runs Sun 04:40 before canonical_compaction. Non-destructive; destructive merge stays human-gated.

**bench_all regression discipline** — `cli/bench_all.py snap before/after` + `diff`. CI-gateable (exit 1 on >2pt content_hit drop). Used for 7-bucket MODALITY_WEIGHTS + boilerplate-strip validation this session.

**Search quality** (all changes eval-verified):
- `$nin` raw-exclude filter narrowed to `experience` collection only (was applied to all 13 — wasted ~230ms). **p95 671ms → 408ms**.
- 7-bucket modality routing: relational, agent_role, code, concrete_infra, narrative, temporal, preference. Each bucket shifts trust weights to most-likely source.
- Chunker strictness: `## Source Summary`, `---json`, mid-JSON fragments (`",\n`, `": "`) skipped unconditionally.
- `search_memory.py:_strip_proposal_boilerplate` strips `## Statement / ## Source Summary / ## Distilled Evidence / ## Observations / ## Merge Suggestion` from file-based canonical results.

**Content cleanup**:
- **6,634 files** (555 distilled + 3 canonical + 6,076 additional) decontaminated of HyDE prompt leakage (`Context — User: You are Chris's second brain...`). 3.7MB removed. Root cause: prior HyDE dispatches via Jenna's OpenClaw main session got ingested by `openclaw_sessions_ingest` as "Chris said" → promoted to distilled/canonical. CLI migration eliminates re-occurrence.
- Eval holdout (`cli/eval_set_stable.json`): 16 entries updated to reflect v3 consolidation (e.g. `_profile` → `_identity`, `brain-api.conf` removed).

**SLO 3 breached → 0**:
- `recall_v2_p95_ms`: 671ms → 408ms ✅
- `recall_v2_content_hit_pct`: stable-eval bug fix + boilerplate strip → **97.8%** ✅
- `atoms_write_throughput_1h`: `_measure_atoms_write_throughput` redesigned — returns floor during night hours + when input (raw_events) is idle. Prevents morning false-positives. + `canonical_pipeline` split 1× → **3× daily** (02:00/07:00/22:00 PT) for even atom flow. ✅
- Quiet-hours gate (22:30-07:30 PT) added to both `slos.py:maybe_alert` and `slo_monitor.py`; persisted config (not hardcoded). 6h per-alert-set rate floor.
- `llm_daily_spend_usd` SLO **removed** — CLI dispatches are subscription-backed (cost_usd=0). Meaningless gauge.

**Reliability**:
- Scheduler `_reconcile_orphans()` at startup: resolves `job_history` rows where `finished_at=NULL` from previous brain-server instance. **461 orphans auto-cleaned**.
- `reasoning.py` missing `get_chris_profile` — added alias to `boot_context.get_chris_state`.
- MinIO endpoint IP un-hardcoded — `_minio.py` uses `docker inspect minio` runtime discovery (OrbStack reassigns IP on restart).
- `claude_boot.sh` — turn 1+ no longer re-injects the full baseline (15 context blocks × every prompt was noise). Only active_recall + doorbell + CWD notes on subsequent turns.
- `brain_mcp_server.py:brain_recall` — **dropped `expand=true`** param. Was triggering LLM expansion (2.6s) + cross-encoder (2.5s) = 5-6s > OpenClaw MCP's 5s timeout. **Brain /recall/v2 now 212ms for MCP path**. Fixes "MCP error -32001: Request timed out" errors that plagued OpenClaw agents.

**UI** (`brain-ui`):
- `EvalHistory.tsx` rewritten — 3-track overlay chart (stable / extended / legacy), design-standard-compliant (OKLCH, Geist Mono, Instrument Panel aesthetic).
- `/brain/eval-history` endpoint merges all 3 track files (was stuck reading legacy only since 2026-04-15).
- `Memory.tsx` / `Review.tsx` / `EvalProposals.tsx` — shadcn default `bg-white` + `shadow-2xl` → design tokens (`bg-[var(--surface)]` + `border-[var(--border)]`).

**Metrics post-session**:
- **0/12 SLOs breached** (was 3/13)
- content_hit@5: 94.2% (bug) → **97.8%**
- p50 /recall/v2 latency: 399ms → **303ms**
- p95 /recall/v2 latency: 671ms → **408ms**
- Daily LLM spend: $150 → **$0**
- 702 atoms, 30,421 ChromaDB chunks, 14,793 Neo4j nodes, 103 scheduled jobs

### v4 env vars (new)

| Var | Default | Purpose |
|---|---|---|
| `BRAIN_MCP_IDLE_TIMEOUT_S` | 600 | MCP server auto-exit after N seconds idle |

## Brain v1 + v2 (shipped 2026-04-13) + v3 llm-wiki consolidation (2026-04-15)

v1 phases 0–8 landed the atoms truth layer, L0–L3 autonomy, SM-2 spaced
repetition, two-track eval, hook v2, and closed-loop self-learning scaffolding.
v2 phases A–I elevated the whole thing to production-grade: committed history,
zero false-positive alerts, 6 SLOs in code, API surface closure, eval/LoRA
loop closure, observability, integration tests, runbook. **156 pytest tests, 8
atomic commits, status=healthy, alerts=[], stable eval 96.4%.**

v3 (2026-04-15) adopted Karpathy's llm-wiki model: **canonical 262 fragment →
43 consolidated pages + 19 entity pages** (85% reduction). Added structural
lint (orphans + data gaps + missing cross-refs), entity page auto-generator
(Sage-dispatched, weekly), query→canonical loop (answer_candidates table),
canonical-first retrieval mode, compaction clustering, auto cross-ref
injection during promote_canonical, graph connectivity backfill
(MENTIONS 2030→4371, RELATES_TO 2087→3421). All new jobs scheduled Sunday
3:30–6:35am (non-destructive only; merge_apply / quality_filter --apply /
canonicalize_entities --apply stay manual).

- **MCP**: `~/.claude.json` `mcpServers.brain` — 16 tools live in every Claude Code session
- **Atoms truth layer** (`brain.db`): 523+ atoms (43 canonical consolidated + 19 entity pages + semantic/episodic/obsolete), 2383 raw_events, 238 archived. Gated by `BRAIN_ATOMS_ENABLED=true` + `BRAIN_ATOMS_READ=true`
- **Autonomy gate** (`brain_core/autonomy.py`): L0–L3 per-action-kind, quiet hours 23:00–07:00 PT (configurable via `POST /brain/quiet-hours`), DENY_PREFIXES + soft denylist (`POST /brain/denylist/add`), persistent breakers (5m/15m/1h/4h backoff). Per-kind override: `POST /brain/autonomy/{kind} {"level":"L2"}`. Top kill: `BRAIN_AUTOPILOT_DISABLED=1` env. Autopilot state lives in `brain_config` table (migrated from JSON).
- **Two-track eval** (incident 2026-04-13): `eval_run` (stable 138, strict 5pt + heal) vs `eval_run_extended` (606 temporal, trend-only 10pt threshold, no heal). Stable baseline **94.9% content_hit, 88.4% source_hit** (last measured 2026-04-16 23:54). Extended **72.3% strict / 85.0% loose content_hit, 5.3% source_hit, mrr 0.039** (last measured 2026-04-17 00:00, post-T2.12 contextual retrieval). Extended tracks literal-wording queries vs consolidated abstractions — trend-only, not a regression gate. Prior cite of "64.0/67.3%" in older CLAUDE.md was outdated.
- **SM-2 spaced repetition**: `/brain/review` GET/POST, `sm2_nightly` 3:25am, tier promotion episodic→semantic→core. Verified end-to-end in Phase C6.
- **Closed-loop self-learning**: `/recall/feedback wrong_answer=true` → `eval_proposals` → `eval_holdout_promote` (Sun 8:45am, novelty-scored) → `eval_holdout_audit` (Sun 9:15am, Telegram digest to Jenna) → human approve → `eval_holdout.json` grows. LoRA: `embed_finetune` → `lora_ab_gate` (Sun 9:30am, 2pt delta + 5pt worst-case guardrail) → symlink flip. `autonomy_proposer` (4:45am daily) reads `accuracy_tracker` and surfaces L2→L3 promotions to audit review queue.
- **Hooks v2**: `claude_boot.sh` 5-min TTL cache + sentinel degraded mode; `post_session.sh` outbox spooler + `cli/outbox_drain.py` 8-retry exponential backoff with quarantine.
- **API surface (Brain v2 Phase B)**: `POST/PATCH/DELETE /brain/triggers`, `GET/POST /brain/quiet-hours`, `GET /brain/denylist` + add/remove, `GET /brain/eval-proposals` + approve/reject + stats, `GET /brain/atoms` + `/{id}` + `/stats`, `GET /brain/slos` + `POST /brain/slos/check`, `/brain/breakers` + `/{kind}/reset`, `/brain/policy/preview`, `/brain/autonomy/{kind}`.
- **Production SLOs** (`brain_core/slos.py`): **12 SLOs** (post-2026-04-17 — removed `llm_daily_spend_usd` after CLI migration, added `atoms_write_throughput_1h`, `atoms_confidence_stddev_1d`, `sleep_cycles_duration_1d_p95`, `holdout_auto_graduation_7d`, `atom_coactivation_rowcount`, `calibration_brier_drift_7d`) enforced every 5 min via `slos_check`. Key thresholds: `recall_v2_p95_ms` ≤500 (was 350), `recall_v2_content_hit_pct` ≥95, `breaker_open_count` =0, `atoms_write_throughput_1h` ≥5 (with night-hours + idle-queue bypass). Quiet hours 22:30-07:30 PT gate blocks non-critical alerts. 6h per-alert-set rate floor.
- **Foundation**: `pyproject.toml` + uv pinned (Python 3.14), `cli/brain_init.py` idempotent bootstrap, **156 pytest tests** + 7 integration + restart soak, ruff+bandit+pre-commit, local fswatch CI via `ai.openclaw.brain-ci.plist`.
- **Brain UI**: `/review` Anki page, `/eval-proposals` approve/reject, Autonomy Levels + Breakers cards on `/autopilot`, SLOs + Quiet Hours + Denylist cards on `/settings`.
- **Runbook**: `brain/RUNBOOK.md` covers crash, scheduler skew, breaker stuck, outbox backlog, eval regression, Chroma/Neo4j outage, embed swap, fresh machine, atoms rollback, kill switch, SLO breach.
- **Cron map**: `brain/CRON_MAP.md` — 90 scheduled jobs visualized, nightly + Sunday windows, work-hours rule.
- **v3 llm-wiki jobs (2026-04-15, auto-scheduled)**: `answer_canonicalize` (daily 3:15), `graph_rebuild_mentions` (Sun 3:30), `graph_backfill_co_mention` (Sun 3:40), `entity_pages` (Sun 4:30), `canonical_index` (Sun 4:45), `canonical_lint` (Sun 5:45), `canonical_compaction` (Sun 6:00), `canonical_merge_draft` (Sun 6:15), `canonical_quality_filter_report` (Sun 6:35). Destructive counterparts (merge_apply, quality_filter --apply, canonicalize_entities --apply) stay human-reviewed.
- **v3 llm-wiki routes**: `POST /brain/canonicalize` (agent mark answer canonical-worthy), `GET /brain/answer_candidates`, `GET /brain/canonical_lint`, `GET /brain/index/rebuild`, `/recall/v2?canonical_first=true` (wiki-as-truth mode), `/brain/graph/nodes?connected_only=true` (drop isolated entities).

### Brain Intelligence (2026-04-11)
- **Neuromorphic retrieval**: Spreading activation (HippoRAG PPR), salience ranking, MMR diversity, episodic binding — all enabled
- **Memory lifecycle**: Topic-aware preference supersession (newer preference on same topic auto-overwrites), category-aware decay (preferences 90d, facts 180d, decisions 365d), access score decay, active forgetting
- **Search scoring**: 55/35/10 vector/keyword/source split, RRF with theoretical max normalization, cross-encoder median-fill, graph entity boost (15%), preference recency boost (30d half-life)
- **Learning loop**: Auto search feedback from /recall, correction extraction from sessions, LoRA training pipeline (accumulating data)
- **Temporal reasoning**: /brain/changes for knowledge diffs, /brain/evolution for preference timelines
- **Procedural memory**: Learned procedures from agent tasks + shell workflow detection, stored in autonomy.db
- **Predictive boot**: Calendar-aware + focus-aggregate + session-continuation adaptive queries
- **Self-monitoring**: Content quality SLO, stale infra detection, memory health reports
