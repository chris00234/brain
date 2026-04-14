# Brain v2 — Cron Map

Visual schedule of every recurring job in the brain process.
All times are local (`America/Los_Angeles`). Source of truth: `brain_core/scheduler.py`.

## Nightly window (2:00 – 5:00 am)

| Time | Job | Owner | Purpose |
|---|---|---|---|
| 02:00 | `canonical_pipeline` | system | inbox → distilled → canonical promotion |
| 02:30 | `memory_lifecycle` (Sun) | system | age-out + extract durable memories |
| 02:45 | `brain_reflect` | sage | contradiction detection in semantic_memory |
| 02:50 | `graph_consolidation` | system | Ebbinghaus decay + LTP + cluster |
| 03:00 | `confidence-drift` | system | unreinforced atom decay |
| 03:05 | `entity_resolution` | system | Neo4j entity merge (auto >0.95, review 0.90–0.95) |
| 03:15 | `neo4j_backup` | system | Neo4j data backup |
| **03:25** | **`sm2_nightly`** | **system** | **SM-2 review scheduler — seeds next_review_at, obsoletes stale** |
| **03:30** | **`eval_run`** | **system** | **Two-track stable eval (138 queries) — strict gate + heal dispatch** |
| **03:50** | **`eval_run_extended`** | **system** | **Two-track extended eval (606 queries) — trend only** |
| 04:00 | `log_rotation` | system | truncate logs >512 KB |
| 04:00 | `profile_regen` (Sun) | sage | rebuild Chris profile from canonical |
| 04:30 | `purge` | system | raw_events retention sweep |
| **04:45** | **`autonomy_proposer`** | **system** | **Phase 7: surface autonomy promote/demote proposals** |
| 04:45 | `evolution` (1st of month) | system | monthly synthesis + reflection |

## Sunday morning window (8:30 – 10:30 am)

| Time | Job | Owner | Purpose |
|---|---|---|---|
| 08:30 | `embed_finetune` | system | LoRA training pair generation + train |
| **08:45** | **`eval_holdout_promote`** | **system** | **Phase C1: novelty-score eval candidates, promote top-N to pending** |
| 09:00 | `eval` (weekly cloning quality) | system | full cloning quality test |
| **09:15** | **`eval_holdout_audit`** | **jenna** | **Phase C2: Telegram digest of pending candidates for human review** |
| **09:30** | **`lora_ab_gate`** | **system** | **Phase 7: LoRA A/B gate + deploy (2pt delta + 5pt worst-case guardrail)** |
| 10:00 | `dead-atom-alert` (Sun) | system | 60-day unreinforced atom alert |
| 10:30 | `health-lint` (Sun) | system | weekly health lint |
| 11:00 | `preference-inference` (Sun) | system | derive preferences from decisions |
| 11:00 | `autonomy-ledger` (Sun) | system | weekly autonomy audit |

## Daily proactive (split across day)

| Time | Job | Purpose |
|---|---|---|
| 07:30 | `proactive-morning` | morning pre-intelligence |
| 13:30 | `proactive-afternoon` | afternoon pre-intelligence |
| 19:30 | `proactive-evening` | evening pre-intelligence |
| 22:00 | `recall` | nightly active recall (Telegram) |

## Frequent (interval-based)

| Cadence | Job | Purpose |
|---|---|---|
| 2 min | `sync-server-ops` | server state file scrape |
| **5 min** | **`slos_check`** | **Phase E1: SLO budget check + Telegram alert on breach** |
| **5 min** | **`outbox_drain`** | **Phase 2: SessionEnd outbox replay** |
| 5 min | `drain-openclaw-queue` | OpenClaw distill drainer |
| 30 min | `distill-observations` | CC observations batch compress |
| 1 h | `reflection-sweep` | outcome-based atom reinforcement |
| 1 h | `wm-expire` | working memory TTL expiry |
| 2 h | `sync-gmail` | Gmail API |
| 6 h | `sync-apple-notes` | Apple Notes |
| 6 h | `sync-claude-code` | Claude Code history.jsonl |
| 6 h | `sync-notion` | Notion 2 workspaces |
| 6 h | `sync-gcal` | Google Calendar |
| 12 h | `sync-onenote` | OneNote local FTS |

## Job count

68 total scheduled jobs as of 2026-04-13 after Phase J.

The exact count comes from `len(brain_core.scheduler.JOB_SCHEDULE)`. To
re-derive after a phase, run:

```bash
.venv/bin/python -c "import sys; sys.path.insert(0, 'brain_core'); from scheduler import JOB_SCHEDULE; print(len(JOB_SCHEDULE))"
```

## Maintenance windows

- **No heavy Ollama/Chroma jobs between 9am–6pm PST** (work hours rule).
  Enforced by `brain_core/autonomy.py` `EXECUTION_WINDOWS["heal.reindex"] = ["night"]`.
- **Quiet hours**: 23:00–07:00 PT. L3 actions get auto-demoted to L2 unless
  in the exception list (`heal.log_rotate`, `heal.vacuum_embed_cache`).
- **Reindex 2× daily**: 03:30 + 23:00 (off-hours).
- **Personal ingest 3× daily**: 06:00, 14:00, 22:00.

## Failure handling

- Every job has `misfire_grace=900` (15 min) by default.
- Failed jobs land in `scheduler_failures` on `/brain/health`.
- Repeated failures trigger the persistent breaker for the action_kind
  (e.g. `heal.reindex`) and back off 5m → 15m → 1h → 4h.
- See RUNBOOK.md §2 for recovery.
