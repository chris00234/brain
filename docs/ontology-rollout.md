# Brain Ontology Rollout

Status as of 2026-04-24: production uses full typed ontology expansion only as a bounded sidecar, not as a main-query rewrite.

Current production config:

- source: `neo4j`
- mode: `sidecar`
- sidecar limit: `2`
- relations: `has_agent,owned_by,owns,proxies,depends_on,manages,has_event,prefers`
- conditional relation activation: enabled
- max expansion terms: `5`
- adaptive sidecar guard: exact fact/config lookups skip sidecar unless the query has explicit relationship intent
- live smoke retries: `1` retry for slow live cases, to avoid failing a rollout on a single cold/concurrent-load outlier while still failing repeated latency regressions
- required gates: ontology relation audit, stale current-truth audit, policy, retrieval A/B, live smoke

## Architecture rule

Neo4j is the instance graph. Ontology is the contract/policy layer. Full ontology relations may be stored in Neo4j, audited, and used by graph search, but query expansion must pass rollout gates before production use.

Two expansion modes exist:

- `rewrite`: appends ontology terms to the main query. This is strict and can perturb provenance/ranking.
- `sidecar`: preserves the original query and runs a small auxiliary RAG query with ontology terms. This is the approved full typed production path.

The sidecar is adaptive: queries that look like exact fact/config lookups
(`port`, `server block`, `configuration`, endpoint/path/version/error/status
code, etc.) do not run ontology sidecar candidates unless they also contain
explicit relationship intent (`owner`, `agent`, `proxy`, `dependency`,
`manages`, `prefers`, etc.). This keeps ontology from perturbing provenance on
precise infra lookups while preserving relationship-aware recall.

## What remains

1. Keep full ontology out of main-query rewrite until it stops causing source/content regression.
2. Improve relation data quality and query intent coverage in small increments.
3. Re-run candidate sweeps after each relation backfill or policy change.
4. Promote only relation sets that pass all gates with no content/source regression and acceptable latency.

## Apply path

Never edit launchd environment variables directly for ontology expansion. Use:

```bash
uv run python cli/apply_ontology_expansion.py \
  --relations has_agent,owned_by,owns,proxies,depends_on,manages,has_event,prefers \
  --source neo4j \
  --mode sidecar \
  --sidecar-limit 2 \
  --conditional \
  --enabled
```

The apply script:

1. Runs `cli/ontology_rollout_gate.py` before touching launchd.
2. Backs up repo and installed Brain server plists under `.omx/plans/`.
3. Patches both plists.
4. Runs `plutil -lint`.
5. Reloads launchd with `bootout`, `bootstrap`, then `kickstart` so env changes are actually loaded.
6. Checks `/healthz`.
7. Runs the rollout gate again.
8. Restores backups and reloads launchd if the post-gate fails.

## Scheduled gate

The scheduled LaunchAgent `ai.openclaw.ontology-rollout-gate` runs daily at 04:45 with the same production config:

```bash
uv run python cli/ontology_rollout_gate.py \
  --relations has_agent,owned_by,owns,proxies,depends_on,manages,has_event,prefers \
  --mode sidecar \
  --sidecar-limit 2 \
  --conditional \
  --live-retries 1 \
  --live-retry-sleep 1.0 \
  --json
```

Artifacts are written under `logs/ontology-gates/`, with the latest report at `logs/ontology-gates/ontology-rollout-latest.json`.

## Stale current-truth guard

The old `canonical_staleness_check` job only caught narrow code-reality claims
in `distilled/*.md`, such as a missing import claim after the code already
added the import. It did not know that an infra substrate had been superseded,
so it could not flag active canonical claims like "ChromaDB is the current
vector database" after the 2026-04-21 Qdrant cutover.

Current guardrails:

- `config/decommissioned_terms.json` defines explicit supersession maps such as `ChromaDB -> Qdrant`.
- `cli/audit_stale_current_truth.py --fail-on-blockers` scans active canonical notes for decommissioned terms used as current-state facts.
- `cli/audit_stale_current_truth.py --scan-vector --apply` scans Qdrant-backed vector collections and marks stale active-current points obsolete instead of deleting them.
- `brain_core/search_unified.py` suppresses stale active-current claims at retrieval time unless the caller explicitly asks for history.
- `brain_core/canonical_staleness.py` runs the same current-truth audits in the daily 04:30 stale job, exits non-zero on canonical blockers, and marks vector blockers obsolete.
- `cli/ontology_rollout_gate.py` runs the audit before policy/retrieval/live gates.
- Historical/superseded/archived mentions are allowed; active current-state claims are blockers.

This is broader than an infra-only cleanup path: any future fact/domain
supersession can be added to `config/decommissioned_terms.json`, and the same
canonical, vector, retrieval, and rollout-gate behavior applies. The important
contract is brain-like replacement: old claims stay auditable as history, but
they stop competing as current truth.

Do not rename compatibility fields like `chroma_id` during this cleanup path.
Those are API/schema compatibility names and require a separate migration plan,
not a stale-truth text cleanup.

## Candidate evaluation path

Before trying to apply a wider relation set or a different mode:

```bash
uv run python cli/ontology_candidate_sweep.py --json
```

Accept only candidates with:

- `content_hit_pct` delta >= `0.0`
- `source_hit_pct` delta >= `0.0`
- p95 latency regression <= `10%`
- mean latency regression <= `25ms`
- ontology expansion p95 <= `75ms`

## Current rejected candidates

The typed sweep on 2026-04-24 rejected full typed `rewrite` because it caused source regression on infra queries such as:

- `searxng server block`
- `ollama model configuration`
- stale pre-Qdrant exact lookup fixture, now replaced with Qdrant-current eval coverage

The same full typed relation set passed as `sidecar` with `sidecar-limit=2`, so that is the production mode.

## Rollback

Use the timestamped backups written by `cli/apply_ontology_expansion.py` under `.omx/plans/`, copy the repo and installed plist backups back to:

- `launchd/ai.openclaw.brain-server.plist`
- `~/Library/LaunchAgents/ai.openclaw.brain-server.plist`

Then reload launchd env, not just kickstart:

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/ai.openclaw.brain-server.plist || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.openclaw.brain-server.plist
launchctl kickstart -k gui/$(id -u)/ai.openclaw.brain-server
```

## Atoms and broad vector stale-truth closure

The stale-current-truth guard now covers all current-truth surfaces:

- active canonical markdown files,
- Qdrant vector collections,
- SQLite `logs/brain.db` atoms,
- retrieval-time stale-current suppression,
- daily `canonical_staleness_check`,
- ontology rollout gate.

`cli/audit_stale_current_truth.py --scan-vector --scan-atoms --apply` is the
manual all-surface repair command. It marks stale points/atoms obsolete and
repairs superseded atoms that were missing `valid_until`.

The final ChromaDB → Qdrant cleanup left 0 blockers across canonical, vector,
and atoms. Remaining ChromaDB mentions are allowed only when they are explicitly
historical, decommission/migration records, or supersession facts.
