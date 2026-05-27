# AGENTS.md — Brain repository context for Hermes/Claude/Codex

Auto-loaded by Hermes when working in `~/server/brain/`.
Concise — defer to `RUNBOOK.md`, `BRAIN_EVOLUTION_PLAN.md`, and `AGENT_HARNESS.md` for depth.

## What this repo is

Chris's personal canonical memory server. FastAPI on port 8791 backed by SQLite + Qdrant (vector) + Neo4j (graph) + Ollama (embeddings).
Owns durable preference/decision truth for Chris's whole agent stack.

## Architecture (one-line each)

- `server.py` — FastAPI entrypoint, routes mounted from `brain_core/routes/`
- `brain_core/` — atoms store, decision ledger, ingest classifier, recall pipeline
- `brain_core/routes/agency.py` — autopilot, tasks, goals, focus, messages, decisions/feedback
- `brain_core/routes/web.py` — `/web/search` (forwards to SearXNG/Tavily)
- `brain_core/routes/cross_repo.py` — `/brain/cross-repo-recall`
- `brain_mcp_server.py` — thin MCP server proxying brain HTTP for Claude Code + Hermes (stdio)
- `cli/` — operational scripts (server_watchdog, brain_init, outbox_drain, post_session)
- `synthesis/` — distillation, reflect, holdout eval
- `launchd/` — service plist sources of truth, deployed to `~/Library/LaunchAgents/`

## Service inventory (current, post-2026-05-23 namespace migration)

Native via launchd `ai.brain.*`:
- `ai.brain.server`, `ai.brain.qdrant`, `ai.brain.neo4j`, `ai.brain.ollama`
- `ai.brain.backup`, `ai.brain.qdrant-backup`, `ai.brain.docker-volumes-backup`
- `ai.brain.log-rotation`, `ai.brain.orbstack-watchdog`, `ai.brain.watchdog`
- `ai.brain.ontology-gate`

OpenClaw is retired as of 2026-05-23. `ai.openclaw.gateway` archived. Hermes profiles run as `ai.hermes.gateway-{jenna,liz,ellie,sage,market}`.

## Conventions

- Python 3.14 for brain server (homebrew `/opt/homebrew/bin/python3`), Python 3.11 for Hermes (venv `~/.hermes/hermes-agent/venv`).
- `uv` for brain Python deps (pyproject.toml + uv.lock). Do NOT `pip install` outside uv.
- Pre-commit: ruff + bandit + pytest. Run `uv run pytest tests/` before edits.
- Conventional commits: `feat:`, `fix:`, `chore:`, `refactor:`, `docs:`.
- Files <300 lines, functions <40 lines. Big-function decomposition is an active sprint.

## Operational rules

- Heavy Ollama/ChromaDB jobs banned 9am-6pm PST (Chris's production hours).
- Every new service: Docker container + Uptime Kuma + Glance dashboard.
- Bearer auth: `~/.brain/credentials/.personal_webhook_secret` (canonical post-OpenClaw path).
- Brain stop hook: `cli/stop_check.sh` (5s timeout). Reports `No stderr output` on healthy turn.
- SessionEnd hook: `cli/post_session.sh` writes to `~/server/brain/outbox/brain-learn/pending/` (canonical post-2026-05-23).

## SLOs that matter most

- `recall_v2_p95_ms` ≤ 1000 (hot path latency budget)
- `recall_v2_content_hit_pct` ≥ 96 (regression gate; current ~97.8)
- `brain_server_rss_mb` ≤ 3072 (current ~566 MB)
- `breaker_open_count` = 0 (circuit breakers)
- `calibration_brier_drift_7d` ≤ 0.05 (silent miscalibration detector)
- `backup_restore_drill_age_hours` ≤ 192 (currently breached at 999h — restore drill stale)

Run `curl -s -H "Authorization: Bearer $(cat ~/.brain/credentials/.personal_webhook_secret)" http://127.0.0.1:8791/brain/slos` for current state.

## Verification before claiming

`feedback_verify_before_claiming` is a hard rule. Do not assert what brain does or doesn't do without reading code end-to-end. `grep` alone is not enough.

## Common tasks

- Restart brain: `launchctl kickstart -k gui/$(id -u)/ai.brain.server`
- Restart qdrant: `launchctl kickstart -k gui/$(id -u)/ai.brain.qdrant`
- Restart neo4j: `launchctl kickstart -k gui/$(id -u)/ai.brain.neo4j`
- Recall test: `curl -s -H "Authorization: Bearer $(cat ~/.brain/credentials/.personal_webhook_secret)" "http://127.0.0.1:8791/recall/v2?q=<query>&k=5"`
- Store test: `curl -s -X POST -H "Authorization: ..." -H "Content-Type: application/json" -d '{"content":"...","kind":"...","tags":[],"confidence":0.5}' http://127.0.0.1:8791/memory`

## What NOT to do here

- `~/.openclaw/` removed 2026-05-23. Full archive at `~/Archives/openclaw-final-2026-05-23.tar.gz` + selective info at `~/server/brain/archived/openclaw-2026-05-23/`. Do not reference legacy paths in new code.
- Do NOT change `~/server/brain/launchd/*.plist` Labels without also updating deployed copy in `~/Library/LaunchAgents/`.
- Do NOT add new top-level launchd labels under `ai.openclaw.*` — that namespace is retired. Use `ai.brain.*` or `ai.hermes.*`.
- Do NOT bypass `cli/ingest_classifier.py` on `/memory` POST — it sets topic_key/speaker_entity/scope used by retrieval filters.
