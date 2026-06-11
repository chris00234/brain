# Brain architecture / DB map — 2026-06-11 structural quality pass

Snapshot from the 2026-06-11 architecture + DB inventory (claude lane
`claude3-brain-arch-db-fable`). Numbers are live measurements from that day,
not estimates. Companion to `STORAGE_MAP.md` (storage rules/retention) — this
doc maps the moving parts and records the structural-quality findings.

## Runtime

Native launchd services (no Docker in prod; `docker-compose.yml` is a Linux
template): `ai.brain.server` (FastAPI, port 8791, `.venv/bin/python
server.py`, KeepAlive on crash), `ai.brain.qdrant` (6333),
`ai.brain.neo4j` (bolt 7687), `ai.brain.ollama` (11434, embeddings only),
`ai.brain.personal-webhook`, plus backup/log-rotation plists. Scheduler:
APScheduler, 163 scheduled jobs / 174 registry entries, resource budget
caps (heavy:1, llm:1, embedder:1, index:1). MCP bridge:
`brain_mcp_server.py` (21 tools full profile / 5 minimal), 4s inner timeout
to fit the 5s MCP window — LLM-backed tools (brain_reason, brain_decide…)
structurally time out and return hints; HTTP is the reliable path for those.

## Storage layers

**SQLite (`logs/`)** — truth + executive + observability:

| DB | Size | Dominant tables | Role |
|---|---|---|---|
| `brain.db` | 553 MB | entry_chunks 211 MB (133K rows), action_audit 118 MB (466K), entry_documents 77 MB (36K), atoms 4 MB (4,114) | Atoms truth layer (34-col `atoms`: supersession links, valid_from/until, hygiene fields), provenance (1,697 edges), Bayesian `atom_evidence` (5,215), entry manifests, recall judgments. Migration chain v15 (`migrations_brain_db.py` + `schema_versions.db`); many observability tables created inline outside the chain. |
| `autonomy.db` | 571 MB | decision_ledger 375 MB (218K rows, big JSON cols), autonomy_decisions 62 MB (598K, 14d retention) | Executive state: tasks/goals/outcomes, working memory, procedures, episodes, decision ledger. |
| `metrics_history.db` | 198 MB | metrics_snapshots (4,322, 14d) | SLO/trend observability. |
| `embedding_cache.db` | 191 MB | query/doc e5 vectors | 30d/15K prune daily. |
| smaller | — | facts.db (32 KB, **1 row, dead layer**), audit.db, llm_usage.db, scheduler_history.db, hyde_cache.db, llm_backlog.db, profile_hypotheses.db, reasoning_checkpoints.db, self_heal_state.db | |

**Qdrant** (sole vector backend since 2026-04-21 Chroma cutover): 9
collections, ~86.7K points, 1024-dim e5 + BM25 sparse, int8 quantization.
canonical 24.3K (3 named vectors dense/contextual/raptor), code 26.8K,
experience 14.2K, distilled 14.2K, semantic_memory 3.3K, obsidian 2K,
knowledge 1.6K, personal 348, healthcheck_probe 0. Shadow chunks
(`*__shadow_v2`) live in the same physical collection behind a payload
discriminator; 5 legacy collection names are payload-aliased.

**Neo4j**: 8,345 Entity / 35,595 MemoryAccess / 180 Skill / 60 Lesson nodes;
43K MENTIONS + 34K RELATES_TO (Hebbian weights). The SQLite fallback
(autonomy.db entities) holds 170 entities — ~2% of Neo4j; a Neo4j outage
effectively disables graph search/boost/exclusion.

## Write path (POST /memory)

test_gate → Ollama embed → `memory_operations.classify_operation`
(ADD/UPDATE/DELETE/NOOP; dup ≤0.05 cosine, update ≤0.15, preference 0.40) →
Qdrant upsert (wait=true) → `ingest_mirror.mirror_memory` (30-word atoms
gate, classifier for topic_key/speaker/scope, SQLite atom upsert, semantic
supersession) → contradiction check → corroboration probe (Bayesian logit
evidence) → action_audit.

Supersession controls: explicit `replaces` (bypasses gates) >
topic_key+speaker cosine gate (expire <0.70, coexist 0.70–0.85, restate
≥0.85) > nightly `conflict_surfacer` + `auto_resolve_stale_contradictions` >
daily near-dedup (cosine <0.10 ∧ Jaccard >0.5, 2,000-point scan cap).

## Read path (/recall/v2 and active_recall — both via `search_unified.search_all`)

Fan-out (rag/canonical/obsidian/graph/FTS, bilingual + ontology expansion) →
RRF fuse → **lifecycle filters** → rerank stack → recall governance
(source authority, route guarantees, noise classifiers, temporal_resolution)
→ quality filter → diversify.

Lifecycle filtering as measured 2026-06-11:
- `semantic_memory`: full gate — superseded_by (SQLite-preferred, payload
  fallback), tier=obsolete, valid_until expiry, provisional/conjecture,
  speaker≠chris, session scope, trust<0.3. Each with an explicit
  `include_*` escape hatch.
- `canonical`: `_is_superseded_canonical_result` (status/superseded_by
  markers, history-pattern exempt).
- **all other collections**: only the text-based
  `_is_stale_current_truth_result` net, which re-derives staleness from
  `config/decommissioned_terms.json` per result per query. The 235 points
  that `stale_current_truth --apply` explicitly marked
  (`superseded_by=stale_current_truth:…`, `memory_class=obsolete`) in
  experience(170)/obsidian(48)/knowledge(17, all shadow rows) are suppressed
  only as long as their terms stay in that config — the durable payload
  markers are not honored at read time outside semantic_memory
  (`search_unified.py` gates them under `r_coll == "semantic_memory"`).

## Eval / SLO

- **Deployment gate**: `eval_run_stable` daily 03:31 over
  `cli/eval_set_stable.json` (**144 cases** since 2026-06-11: 138 legacy
  lookups + 6-case categorized quality slice — stale_fact_supersession ×4
  incl. KO + history-preservation, privacy_negative_personal_source with
  forbidden_content enforcement, identity_canon_over_stale_provenance).
  Guarded by `tests/unit/test_eval_set_stable_quality_slice.py`.
  `recall_v2_content_hit_pct` SLO (critical, ≥96%) reads its report.
- Trend tracks: extended 605 (59.5% strict / 80.3% loose), adversarial 18
  weekly (13/18 live 2026-06-11; 4 failures stuck 3 weeks), holdout 10,
  RAGAS 12, train 595 (sweep tuning).
- Closed loop: search-feedback.jsonl (passive serve log) → recall-gaps.jsonl
  → gap_detector (knowledge_gap tasks at ≥3 repeats/14d); eval_proposals
  promotion pipeline (40 promoted / 343 rejected — near-stasis).
- 30 SLOs (`brain_core/slos.py`), 9 quality / 21 ops; 0 breached at audit.

## Backup / restore reality (verified 2026-06-11)

- SQLite: daily 03:10 gz to `logs/backups/` (brain 76 MB, autonomy 30 MB) —
  **only the same-day copy is kept locally**; multi-day retention is MinIO
  `rag-backups` (14d).
- Qdrant: nightly 03:00 tarball `qdrant-backups/` (719 MB + SHA256) +
  per-collection snapshots uploaded to MinIO then cleaned (local snapshot
  dirs are empty).
- Neo4j: nightly 03:15 dump → MinIO.
- **Weekly restore drill (Sat 04:35) is failing 2/3 components** (last run
  2026-06-07: Neo4j → MinIO connection refused; Qdrant restore sub-test
  status=error). SQLite restore path is the only drill-verified one.

## Structural findings (ranked, 2026-06-11)

1. **Eval-coverage blindness (fixed this pass)** — the only blocking gate had
   zero stale-truth/noise/temporal/provenance cases while sitting at 100%
   for weeks. Quality slice promoted from adversarial after 3 consecutive
   live passes; gate green at 144/144.
2. **Read-side ignores explicit invalidation markers outside
   semantic_memory** — suppression of stale_current_truth-marked rows
   depends on per-query text re-scan vs the durable payload markers;
   config drift silently reopens the leak. Candidate next contract.
3. **Restore-drill failures** (MinIO reachability) — backup posture is
   single-local-copy + unverified remote for Qdrant/Neo4j.
4. **Dead/underused layers** — facts.db (1 row, zero recall wiring),
   decision_ledger review loop (218,473/218,515 unreviewed; accuracy_drop =
   81.8% of entries), eval_proposals near-stasis.
5. **Dual-store drift** — Qdrant semantic_memory shows 1,567 non-empty
   superseded_by vs 1,062 in SQLite atoms; mirrors are best-effort by
   design, read path ORs both so it fails safe.
6. Lifecycle numbers that look alarming but are by-design soft invalidation
   (history stays queryable; read-time filters enforce): 1,232 expired-live
   atoms, 992 superseded-live atoms. The real write-time gap (201
   same-topic supersession misses in the 0.70–0.85 cosine window) is
   compensated at read time by `recall_governance/temporal_resolution.py`.

## Schema decision — 2026-06-11

**No schema change.** The 34-column atoms truth layer (supersession links,
bi-field validity, hygiene metadata, provenance edges, evidence ledger,
migration framework at v15) already encodes everything the measured quality
gaps need. Every gap found is wiring/coverage (read-side enforcement, gate
composition, drill connectivity), not missing structure. A migration would
add deploy risk with zero measured benefit.
