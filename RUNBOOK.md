# Brain v2 — Recovery Runbook

Production troubleshooting guide for `brain-server` and dependencies.
Last updated: 2026-04-13 after Brain v2 phases A–H.

---

## 0. Quick health check

```bash
SECRET=$(cat ~/.openclaw/credentials/.personal_webhook_secret)
curl -sf -H "Authorization: Bearer $SECRET" http://127.0.0.1:8791/brain/health | jq
```

Expected: `status=healthy`, `alerts=[]`, `scheduler_failures=[]`.

If anything else, walk the relevant section below.

---

## 1. Brain server crash / not responding

### Symptoms
- `curl http://127.0.0.1:8791/healthz` hangs or fails
- launchd KeepAlive is restarting the process repeatedly
- Recent commits broke startup

### First-line check
```bash
launchctl list | grep brain-server
tail -50 /Users/chrischo/server/brain/logs/server.err.log
```

### Fix
1. Force a clean restart:
   ```bash
   launchctl bootout gui/$(id -u)/ai.openclaw.brain-server
   launchctl bootstrap gui/$(id -u) /Users/chrischo/Library/LaunchAgents/ai.openclaw.brain-server.plist
   sleep 5
   curl -sf -H "Authorization: Bearer $SECRET" http://127.0.0.1:8791/healthz
   ```
2. If still failing, check schema_versions startup migration:
   ```bash
   /Users/chrischo/server/brain/.venv/bin/python /Users/chrischo/server/brain/cli/brain_init.py migrate
   ```
3. If `downgrade_refused` appears, the code is older than the DB. Roll forward
   the code (git pull) or roll back the DB (manual sqlite reset — destructive).

### Verification
```bash
tests/smoke/restart_soak.sh 2  # 2-iteration soak with eval check
```

---

## 2. Scheduler skew / job failures

### Symptoms
- `/brain/health` shows `scheduler_failures` non-empty
- A specific job hasn't run in expected window

### First-line check
```bash
curl -sf -H "Authorization: Bearer $SECRET" http://127.0.0.1:8791/jobs | jq '.registry | length'
curl -sf -H "Authorization: Bearer $SECRET" http://127.0.0.1:8791/brain/health | jq '.scheduler_failures'
```

### Fix
1. Manually trigger the failed job:
   ```bash
   curl -sf -X POST -H "Authorization: Bearer $SECRET" http://127.0.0.1:8791/jobs/<job_name>
   ```
2. Watch its log: `tail -30 logs/jobs/<job_name>.log`
3. If it's a recurring failure, check upstream services (qdrant, ollama, neo4j).

---

## 3. Circuit breaker stuck open

### Symptoms
- An action kind isn't firing even when conditions trigger it
- `/brain/breakers` shows `state=open` for the kind

### First-line check
```bash
curl -sf -H "Authorization: Bearer $SECRET" http://127.0.0.1:8791/brain/breakers | jq
```

### Fix
1. Inspect the breaker state:
   ```sql
   sqlite3 logs/autonomy.db "SELECT * FROM heal_breakers WHERE state='open'"
   ```
2. Identify root cause via `audit_log` (which dispatch failed):
   ```sql
   sqlite3 logs/audit.db "SELECT * FROM audit_events WHERE event_type='autonomy_blocked' ORDER BY timestamp DESC LIMIT 10"
   ```
3. Fix the upstream issue (or accept it as expected).
4. Manually reset:
   ```bash
   curl -sf -X POST -H "Authorization: Bearer $SECRET" "http://127.0.0.1:8791/brain/breakers/<kind>/reset"
   ```

### Backoff tiers
After 3 consecutive failures, breaker opens with cooldown:
- Trip 1: 5 min
- Trip 2: 15 min
- Trip 3: 1 h
- Trip 4+: 4 h
After cooldown → half-open → one probe → closed (success) or open next tier (fail).

---

## 4. Outbox backlog (SessionEnd transcripts piling up)

### Symptoms
- `~/.openclaw/outbox/brain-learn/pending/` growing
- `outbox_pending_count` SLO at warning

### First-line check
```bash
ls ~/.openclaw/outbox/brain-learn/pending/ | wc -l
ls ~/.openclaw/outbox/brain-learn/quarantine/ | wc -l
tail -20 ~/.openclaw/logs/brain-outbox-drain.log
```

### Fix
1. Manually trigger drain:
   ```bash
   /Users/chrischo/server/brain/.venv/bin/python /Users/chrischo/server/brain/cli/outbox_drain.py
   ```
2. Verify brain `/learn` endpoint is responding:
   ```bash
   curl -sf -X POST -H "Authorization: Bearer $SECRET" -H "Content-Type: application/json" \
     -d '{"transcript":"test","source":"runbook","agent":"runbook"}' \
     http://127.0.0.1:8791/learn
   ```
3. If items in `quarantine/` (after 8 retries), inspect them — they're permanently
   failed but kept for evidence. Move to `done/` to give up, or back to `pending/`
   after fixing the root cause.

---

## 5. Eval regression

### Symptoms
- `eval_run` job fails the regression gate
- Telegram alert: `[eval_gate] REGRESSION: hit_content@5 dropped Xpts`

### First-line check
```bash
cat /Users/chrischo/server/brain/cli/eval_baseline_stable.json
sqlite3 /Users/chrischo/server/brain/logs/scheduler_history.db "SELECT * FROM job_history WHERE job_name='eval_run' ORDER BY ts DESC LIMIT 5"
```

### Fix
1. Run stable eval manually to confirm:
   ```bash
   cd /Users/chrischo/server/brain && .venv/bin/python cli/eval_compare.py --eval-set cli/eval_set_stable.json --json --limit 138
   ```
2. If the regression is real, check recent commits for breaking changes:
   ```bash
   git log --oneline --since="3 days ago" -- brain_core/search_unified.py brain_core/rerank.py
   ```
3. If false alarm (eval set changed, baseline stale), refresh the baseline:
   ```bash
   .venv/bin/python cli/eval_gate.py --eval-set cli/eval_set_stable.json --baseline cli/eval_baseline_stable.json --track stable --update-baseline
   ```
4. The two-track gate splits stable vs extended — stable should never drift,
   extended is trend-only. See incident notes 2026-04-13.

---

## 6. Qdrant outage

### Symptoms
- `/brain/health` shows `services.qdrant=down`
- `/recall/v2` returns errors

### First-line check
```bash
curl -sf http://127.0.0.1:6333/readyz
launchctl list | grep qdrant-native
tail -50 ~/server/brain/logs/qdrant-native.err.log
```

### Fix
1. Restart native Qdrant:
   ```bash
   launchctl kickstart -k gui/$(id -u)/ai.openclaw.qdrant-native
   ```
2. Wait 5s, re-check `/readyz`.
3. If persistent failure, check disk space on `~/server/brain/qdrant-data/`.
4. If data corrupt (rare), restore a snapshot from MinIO:
   ```bash
   # Pull latest backup
   mc cp local/rag-backups/qdrant-backup-YYYY-MM-DD.tar.gz /tmp/
   tar xzf /tmp/qdrant-backup-YYYY-MM-DD.tar.gz -C /tmp/
   # Upload per-collection snapshot via PUT /collections/{name}/snapshots/upload
   # Then re-run cli/qdrant_bootstrap.py to recreate payload indexes if needed.
   ```

---

## 7. Neo4j outage

### Symptoms
- `/brain/health` shows `services.neo4j=down`
- Graph search returns empty results
- `entity_graph` reads fall back to SQLite

### First-line check
```bash
launchctl list | grep neo4j
tail -30 /opt/homebrew/var/log/neo4j/debug.log 2>/dev/null
```

### Fix
1. Restart:
   ```bash
   launchctl kickstart -k gui/$(id -u)/ai.openclaw.neo4j-native
   ```
2. SQLite fallback in `entity_graph.py` keeps the brain functional during outage.

---

## 8. MinIO outage

### MinIO is no longer monitored by /brain/health
The probe was removed in Phase A2 (2026-04-13). MinIO is a docker container
on `server-net` for qdrant backups only — brain has no direct dependency.
Container health is verified by docker-compose healthcheck.

```bash
docker ps --filter "name=minio" --format '{{.Status}}'
```

If down: `cd ~/server/minio && docker compose up -d`.

---

## 9. Embed model swap (Ollama)

### Symptoms
- New model deployed, /recall returns wrong dimensionality
- Embed cache hit rate drops to 0

### First-line check
```bash
curl -sf http://127.0.0.1:11434/api/tags | jq '.models[].name'
echo "active model: $BRAIN_EMBED_MODEL"
```

### Fix
1. Set `BRAIN_EMBED_MODEL` in plist and restart brain
2. Wipe embed cache:
   ```bash
   sqlite3 logs/embedding_cache.db "DELETE FROM embed_cache"
   ```
3. Re-index collections:
   ```bash
   curl -sf -X POST -H "Authorization: Bearer $SECRET" http://127.0.0.1:8791/jobs/canonical_index
   ```
4. Run stable eval to verify recall preserved.

---

## 10. Fresh machine bootstrap

### Symptoms
- New Mac, want to run brain from zero state

### Fix
```bash
git clone <brain-repo> ~/server/brain
cd ~/server/brain
uv sync --dev
.venv/bin/brain-init check        # report gaps
.venv/bin/brain-init secrets       # seed webhook secret
.venv/bin/brain-init plists        # install launchd plists
.venv/bin/brain-init migrate       # run schema_versions
launchctl kickstart -k gui/$(id -u)/ai.openclaw.brain-server
curl -sf http://127.0.0.1:8791/healthz
```

Qdrant, Ollama, Neo4j must be installed separately (not packaged with brain).

---

## 11. Atoms layer rollback

### Symptoms
- atoms_store writes are corrupt or filtering wrong results
- Need to disable the truth layer urgently

### Fix
1. Set env var in plist:
   ```xml
   <key>BRAIN_ATOMS_READ</key><string>false</string>
   ```
2. Optionally also disable writes:
   ```xml
   <key>BRAIN_ATOMS_ENABLED</key><string>false</string>
   ```
3. Bootout/bootstrap to apply:
   ```bash
   launchctl bootout gui/$(id -u)/ai.openclaw.brain-server
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.openclaw.brain-server.plist
   ```
4. Brain falls back to Qdrant payload metadata for tier/supersession.
5. The brain.db is not deleted — re-enabling the flag picks up where it left off.

---

## 12. Autonomy gate kill switch

### Symptoms
- Brain is doing something it shouldn't (autonomous action gone wrong)
- Need to stop ALL autonomous behavior immediately

### Fix
```bash
# Top-level kill switch (env var, hardest)
launchctl setenv BRAIN_AUTOPILOT_DISABLED 1
launchctl kickstart -k gui/$(id -u)/ai.openclaw.brain-server
```

Or via API (graceful, persists in brain_config):
```bash
curl -sf -X POST -H "Authorization: Bearer $SECRET" -H "Content-Type: application/json" \
  -d '{"enabled":false,"confidence_threshold":0.8,"updated_by":"runbook"}' \
  http://127.0.0.1:8791/brain/autopilot
```

To re-enable, unset the env var or set `enabled=true`.

---

## 13. SLO breach summary

| SLO | Target | Severity | Action on breach |
|---|---|---|---|
| `recall_v2_p95_ms` | ≤ 350ms | warning | Investigate latency: check Qdrant load, embed cache hit rate, cross-encoder enabled |
| `recall_v2_content_hit_pct` | ≥ 95% | critical | Run stable eval, compare baseline. Rollback recent search changes if regressed |
| `breaker_open_count` | 0 | critical | See section 3 |
| `outbox_pending_count` | ≤ 20 | warning | See section 4 |
| `atoms_write_fail_rate_1h` | ≤ 1% | warning | Check `audit.db` for `atoms_write_fail` events. Inspect brain.db disk space |
| `eval_holdout_growth_weekly` | ≥ 0 | info | Info-only, never alerts |

---

## v3 llm-wiki operations (2026-04-15)

### 1. Canonical restructure broke eval scores

Symptom: `eval_gate.py` shows large source_hit regression after canonical consolidation.

Cause: `expected_source` paths in eval sets point to canonical paths that moved to
`canonical/archived/<domain>/`.

Fix:
```bash
cp cli/eval_set*.json{,.pre-reshape.bak}
.venv/bin/python -c "
import json, os, glob
KDIR = '/Users/chrischo/server/knowledge'
for name in ['eval_set.json','eval_set_stable.json','eval_set_extended_v2.json']:
    p = f'cli/{name}'; d = json.load(open(p))
    for c in d:
        exp = c.get('expected_source','')
        if '/' not in exp: continue
        if os.path.exists(os.path.join(KDIR, exp)): continue
        base = os.path.basename(exp)
        alts = glob.glob(f'{KDIR}/canonical/archived/**/{base}', recursive=True)
        if alts: c['expected_source'] = alts[0].replace(KDIR+'/', '')
    json.dump(d, open(p,'w'), indent=2, ensure_ascii=False)"
# Then refresh baselines
.venv/bin/python cli/eval_gate.py --eval-set cli/eval_set_stable.json --baseline cli/eval_baseline_stable.json --update-baseline --track stable
```

### 2. Compaction merge draft produces empty / "Unreadable placeholder"

Cause: cluster members already moved to archived/ by a prior apply; `_find_note_by_id`
in canonical_merge_apply can't locate them.

Fix: Regenerate compaction report before drafting:
```bash
rm reports/canonical_compaction/$(date +%Y-%m-%d).{json,md}
.venv/bin/python synthesis/canonical_compaction.py
.venv/bin/python synthesis/canonical_merge_draft.py --limit 3
```

### 3. Quality filter --apply over-archives real knowledge

Cause: new title patterns (JSON-body notes, reflection outputs) slip through.

Recovery: Archived files are preserved — just move back:
```bash
mv knowledge/canonical/archived/<domain>/<file>.md knowledge/canonical/<domain>/
# Then manually set status back to active in frontmatter
```

### 4. Graph UI shows lots of isolated nodes

Cause: `/brain/graph/nodes` returning top-N nodes + top-M links independently leads
to off-canvas endpoints.

Fix: Already patched in `server.py:graph_nodes_endpoint`. Pass
`?connected_only=true` for zero-isolated view. UI toggle also available
(Graph.tsx: Connected/All button).

### 5. canonicalize_entities --apply merges false pairs (agent names, dates)

Never run without human review. Safeguards added in `cli/canonicalize_entities.py`
(_DATE_RE, _NUMSEQ_RE, _HEX_UID_RE, _SHORT_ENUM_RE, SequenceMatcher ≥0.75 gate)
reject the worst cases, but agent-name collisions (ellie/sage sessions) and
version drops (next.js 16 → next.js) still pass.

Manual workflow:
```bash
BRAIN_ATOMS_ENABLED=true .venv/bin/python cli/canonicalize_entities.py --threshold 0.92
# Review the "Merge plan" output, reject any with different agent/version/date
# Then apply cherry-picked pairs manually via Neo4j
```

---

## Reference

- Brain entry: `~/server/brain/server.py` (FastAPI on :8791)
- Services: qdrant (native, 6333), ollama (native, 11434), neo4j (native, 7687)
- Schema migrations: `~/server/brain/brain_core/schema_versions.py`
- Autonomy gate: `~/server/brain/brain_core/autonomy.py`
- Persistent breakers: `~/server/brain/brain_core/breakers.py`
- SLO definitions: `~/server/brain/brain_core/slos.py`
- Outbox drainer: `~/server/brain/cli/outbox_drain.py`
- Restart soak: `tests/smoke/restart_soak.sh`

---

## 16. 2026-04-16 Tier 1/2/3 module recovery

New subsystems shipped this cycle. Each has its own failure + recovery pattern.

### retrieval_competition table bloat (Bjork inhibition)
- **Symptom:** `retrieval_competition` row count > 500k, inhibition job slow.
- **Fix:** bump VACUUM_AFTER_DAYS in `brain_core/retrieval_inhibition.py` (default 60).
  Or manually: `sqlite3 logs/brain.db "DELETE FROM retrieval_competition WHERE last_seen_at < date('now','-60 days')"`.

### confidence_calibration returning identity
- **Symptom:** `/brain/doubt` output matches raw atoms.confidence; no fit applied.
- **Cause:** fewer than 50 samples from eval holdout.
- **Fix:** add more per-test entries to `eval-report-stable.json` by running `POST /jobs/eval_run_stable` with `--include-per-test`.

### dream_replay emits zero conjectures
- **Cause:** Neo4j has no entity pairs with `size(name)>=4` and `mention_count>=2` unconnected.
- **Fix:** run `graph_rebuild_mentions` + `graph_backfill_co_mention` Sunday jobs to populate entity graph first.

### RAPTOR tree empty
- **Symptom:** canonical collection has no `raptor_level>0` rows after `raptor_build` job (Qdrant stores raptor summaries under the same collection, discriminated by `raptor_level`).
- **Cause:** fewer than 2 × MIN_CLUSTER_SIZE (4) active canonical notes.
- **Fix:** verify `canonical` collection has documents with `status=active`. The job returns status=skip.

### schema_revision firing too often
- **Symptom:** `raw/inbox/` filling with `raw_schema_revision_*` records.
- **Cause:** MIN_CLUSTER_SIZE set too low or many `prediction_error` events.
- **Fix:** bump `MIN_CLUSTER_SIZE` in `brain_core/schema_revision.py` from 3 to 5.

### SSE /recall/stream connection hangs
- **Symptom:** client sees keepalive comments but no events.
- **Cause:** internal `search_all` call blocked. Check `/brain/slos` for `recall_v2_p95_ms` breach.
- **Fix:** stream has a 20s wall-clock cap built in; emits `event: end {"reason":"timeout"}` and closes.

### Self-RAG critique latency spike
- **Symptom:** iterative recalls with `BRAIN_SELF_RAG_ENABLED=true` exceeding 3s.
- **Cause:** Jenna dispatch congested.
- **Fix:** flip `BRAIN_SELF_RAG_ENABLED=false` in launchd plist. Heuristic `_crag_score` stays live.

### Conjectures leaking into factual recall
- **Symptom:** top-K results include atoms with `kind=conjecture` or `dream:*` ids.
- **Cause:** `include_provisional=True` passed to `search_all`.
- **Fix:** default is False. Verify callers aren't overriding.
