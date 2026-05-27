# Brain 5-Year Sustainability Assessment

**Written**: 2026-04-17
**Horizon**: 5+ years of continuous use as Chris's primary durable memory

## TL;DR

Brain is solid today. Main 5-year risks are **data growth without retention, embedding model obsolescence, and schema migration discipline**. The most critical of these (retention + VACUUM) shipped 2026-04-17. The others are documented here with mitigation timing.

---

## Risk Matrix

| # | Risk | Severity | Time-to-Impact | Status | Mitigation |
|---|------|----------|----------------|--------|------------|
| 1 | action_audit unbounded growth | HIGH | 6-12 months | **FIXED 2026-04-17** | `action_audit_retention` job (daily 4:20am, 90d retain) |
| 2 | llm_usage table unbounded | HIGH | 12 months | **FIXED 2026-04-17** | `llm_usage_retention` job (monthly 4:30am, roll up to `llm_usage_monthly`) |
| 3 | SQLite file size creep from deletes | HIGH | 12-18 months | **FIXED 2026-04-17** | `db_vacuum_weekly` job (Sun 5:30am) — now also covers metrics_history.db |
| 1b | autonomy_decisions unbounded growth | HIGH | 30 days | **FIXED 2026-04-26** | `autonomy_decisions_retention` job (daily 4:35am, 14d retain). The table grew 600KB → 81MB in 8 days at ~48K rows/day. Only db_maintenance reads it; nothing on the hot path. |
| 1c | metrics_snapshots safety-net retention | MEDIUM | ongoing | **FIXED 2026-04-26** | `metrics_history_retention` job (daily 4:40am, 14d retain). slos.py only reads the last 20 rows; everything older is observability history. metrics_buffer.persist still does its 90d DELETE as the longer-term ceiling. |
| 4 | Embedding model obsolescence | HIGH | 2-3 years | OPEN | See §Embedding Upgrade Path |
| 5 | Schema migration discipline | MEDIUM | ongoing | PARTIAL | `migrations_brain_db.py` exists, procedure documented §Migrations |
| 6 | Chris's preferences drift over time | MEDIUM | ongoing | PARTIAL | supersede_by + memory_lifecycle handle some. No explicit "retire preference" UI. |
| 7 | Canonical page quality decay | MEDIUM | 18-24 months | PARTIAL | canonical_lint + canonical_design_drift jobs exist; no auto-retirement |
| 8 | Dependency version bumps (Python, Qdrant, Neo4j, Ollama) | MEDIUM | 1-2 years per | OPEN | Locked pyproject.toml; upgrade plan below |
| 9 | API endpoint proliferation without versioning | LOW | 3-5 years | OPEN | ~90+ endpoints today; no /v1 prefix strategy |
| 10 | Backup restore not drilled | MEDIUM | incident-triggered | OPEN | Daily brain-backup runs; RESTORE procedure needs yearly drill |
| 11 | raw_events doubles storage via FTS5 | LOW | 2-3 years | ACCEPTED | FTS5 is critical for extended-eval literal-wording; 2× is worth it |
| 12 | Metrics/cost observability loses detail | LOW | 90 days+ | **FIXED** via monthly rollup | llm_usage_monthly keeps long-term cost history |

---

## Shipped (2026-04-17)

1. **`action_audit_retention`** — daily 4:20am. Deletes rows > 90d. Currently 47926 rows → estimated steady-state ~50K with retention.
2. **`llm_usage_retention`** — monthly 1st 4:30am. Archives >90d to `llm_usage_monthly` (month, agent) rollup. Preserves cost analytics forever, bounds detail table.
3. **`db_vacuum_weekly`** — Sunday 5:30am. `VACUUM + ANALYZE` on brain.db, autonomy.db, llm_usage.db. Reclaims free pages, refreshes query planner.
4. **`/brain/health` now has `growth_stats()`** function (call `db_maintenance.growth_stats()`) returning row counts + DB sizes. Hook it to the UI for Chris's monthly checkup.

---

## Open Risks — Future Action Needed

### §Embedding Upgrade Path (2-3 year horizon)

Current: `blaifa/multilingual-e5-large-instruct` (1024-dim, Korean+English optimized).

In 2-3 years, better multilingual embedders will exist. Upgrade path:
1. Stage new model via Ollama parallel to current
2. Re-embed **one collection at a time** (canonical first — smallest, highest value)
3. Run extended eval delta; accept if ≥ current
4. Flip read-side embed to new model
5. Re-embed remaining collections in off-hours
6. Delete old embedder data

**Trigger signal**: watch MTEB leaderboard quarterly. When a new embedder gains >3pts on Korean+English retrieval, start upgrade.

**Known cost**: full re-embed of 12K chunks × ~1 sec Ollama latency = 4 hours. Do on Sunday maintenance window.

### §Migrations Discipline (ongoing)

`migrations_brain_db.py` exists. Every schema change should:
1. Add a migration function there
2. Bump version number in `brain_config_store`
3. Test idempotency (safe to run twice)
4. Document in `CRON_MAP.md` / `RUNBOOK.md`

**This session violated**: 5 new tables added via `CREATE IF NOT EXISTS` in module-level `_ensure_schema()`. Works today (idempotent), but if future refactoring removes one of those modules, the tables become orphans with no migration record.

**Fix**: add a consolidation migration that records all 2026-04-17 tables (`atom_valence`, `attention_queue`, `contextual_embed_audit`, `raw_events_fts`, `claude_llm_queue`, `llm_usage_monthly`) into `migrations_brain_db.py`. Do in next session — not urgent but prevents drift.

### §Backup Restore Drill (yearly)

Daily backup runs at 3:10am (just shipped). Restore procedure:

```bash
# 1. Stop brain-server
launchctl bootout gui/$(id -u)/ai.brain.server

# 2. Find latest backup
ls -la ~/server/brain/logs/backups/ | tail -5

# 3. Test restore to a temp location first
cp ~/server/brain/logs/backups/brain-YYYYMMDD.db /tmp/brain-restore-test.db
sqlite3 /tmp/brain-restore-test.db "PRAGMA integrity_check"
# Expected: "ok"

# 4. If test passes, replace production (keep old as .bak)
cp ~/server/brain/logs/brain.db ~/server/brain/logs/brain.db.pre-restore
cp /tmp/brain-restore-test.db ~/server/brain/logs/brain.db

# 5. Restart brain-server
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.brain.server.plist
sleep 5
curl -sf http://127.0.0.1:8791/healthz | jq -c
```

**Action**: drill this procedure on a Sunday maintenance window within the next 30 days. Timestamp in RUNBOOK when done.

### §Preference Drift (ongoing)

Chris's atoms from 2026-04 may not reflect Chris in 2031. Current handling:
- `supersede_by` chain replaces old preferences
- `memory_lifecycle` ages out unused atoms (confidence decay)
- Nightly `brain_reflect` surfaces contradictions

**Gap**: no explicit "this preference was true in 2026 but no longer applies" tagging. A 5-year-old preference may still surface in retrieval with high score.

**Mitigation (deferred)**: add `valid_from`/`valid_until` to atoms (already exists!). Populate on explicit revocation via new `/brain/atoms/{id}/retire` endpoint. Defer until demonstrated problem.

### §API Versioning (3-5 year horizon)

~90+ endpoints, no `/v1/` prefix. Breaking changes would ripple to:
- Hermes profiles (brain_* MCP tools and BrainMemoryProvider)
- Telegram bots
- Chris's interactive tools
- Claude Code hooks (boot_context, SessionEnd)

**Mitigation (deferred)**: start prefixing new endpoints `/v1/` from today, keep old ones as aliases until all callers migrate. Not urgent but debt compounds.

### §Dependency Upgrades (every 18 months)

- Python 3.14 → 3.15/3.16 within 2 years
- Qdrant: breaking changes in major versions (seen already)
- Neo4j: schema migrations required
- Ollama: model format shifts possible
- APScheduler: stable

**Mitigation**: yearly audit — run `uv lock --upgrade --dry-run`, test in dev worktree, update RUNBOOK with pins.

---

## What's Working Well

**Self-healing patterns (already shipped)**:
- SLOs with Telegram alerts
- Circuit breakers on LLM dispatch
- Autonomy gate L0-L3 with quiet hours
- Nightly eval regression gate (heal on drop)
- Two-track eval (stable vs extended)

**Data durability**:
- Daily brain-backup + qdrant-backup (independent failure domains)
- SQLite WAL mode
- Qdrant persistent volume

**Observability**:
- /brain/slos 12 metrics
- /jobs scheduler status
- /brain/health composite
- llm_usage.db per-call cost tracking
- Telegram alerts for critical breaches

---

## Yearly Checklist (2027, 2028, ...)

Every April 17 (anniversary of this assessment):

1. [ ] Review `growth_stats()` — any table > 100K rows? Any DB > 500MB?
2. [ ] Review MTEB multilingual leaderboard for embedder upgrade trigger
3. [ ] Run backup restore drill end-to-end
4. [ ] Audit `migrations_brain_db.py` completeness
5. [ ] Audit `uv.lock` for major-version bumps available
6. [ ] Review Python/Qdrant/Neo4j/Ollama versions vs latest stable
7. [ ] Spot-check 10 random canonical pages — still relevant? still accurate?
8. [ ] Read 12 months of `llm_usage_monthly` — any anomalies, trends?
9. [ ] Review `action_audit_retention` and `llm_usage_retention` actual vs expected behavior
10. [ ] Take 30-min break. Think about what Chris wants from the brain now vs when it was built. Anything missing?

---

## Explicit NOT Doing (rationale)

- **Multi-user support**: brain is explicitly Chris-only. Adding multi-tenancy = 10x complexity for 0 value.
- **Cloud sync / distributed brain**: single-node on Chris's Mac Studio is by design (privacy, latency, control). Only reason to change: if Chris gets multiple permanent devices.
- **GraphQL API**: REST + MCP are fine. GraphQL adds schema maintenance burden without obvious value here.
- **Transformer-based auto-merge**: canonical merges use LLM critique but not a trained model. The training data is too thin and task is too rare.

---

## Summary Judgment

**Is brain 5-year-ready? YES, with caveats.**

- Data growth: NOW controlled via retention + VACUUM ✓
- Embedding obsolescence: known upgrade path, trigger-watched ⚠ (needs yearly check)
- Schema discipline: mostly good, one debt item (migrations table sync) ⚠
- Backup: shipped, restore needs drill ⚠
- Preference drift: handled automatically, no red flags ✓
- API stability: no versioning but callers are all internal ⚠ (low priority)

**Highest leverage 2026-04-18+ action**: the yearly checklist above. Add it to Chris's calendar as a recurring reminder.
