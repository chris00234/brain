# Brain — Deployment

**Status (2026-04-21)**: Chris runs the brain + all storage backends natively on his M4 Max Mac Studio via launchd. Qdrant is built from source (v1.17.0 via cargo) and supervised by `ai.openclaw.qdrant-native`. The deployment artifacts in this document (`Dockerfile`, `docker-compose.yml`) are a portable fallback for Linux — they prove the brain can be containerized, run on a fresh Linux box, and recovered from cold-start in under 5 minutes.

## Deployment modes

### Mode A — Native macOS via launchd (production today)

Chris's box runs nine launchd services:

| Service | Plist | Purpose |
|---|---|---|
| `ai.openclaw.brain-server` | `~/Library/LaunchAgents/` | Brain FastAPI on :8791 (KeepAlive supervisor) |
| `ai.openclaw.qdrant-native` | same | Qdrant v1.17 on :6333 / :6334 (source-built binary at `~/.local/bin/qdrant`) |
| `ai.openclaw.ollama-native` | same | Ollama on :11434, Apple Silicon GPU/NE |
| `ai.openclaw.neo4j-native` | same | Neo4j Bolt :7687 / HTTP :7474, 512MB heap |
| `ai.openclaw.qdrant-backup` | same | Independent failure domain backup loop (Qdrant snapshots + knowledge tree) |
| `ai.openclaw.gateway` | same | OpenClaw gateway on :18789 |
| `ai.openclaw.orbstack-watchdog` | same | Docker auto-recovery |
| `ai.openclaw.watchdog` | same | Gateway watchdog |
| `ai.openclaw.log-rotation` | same | Daily log compression |

Recovery from cold:
```bash
launchctl bootout gui/$UID/ai.openclaw.brain-server
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/ai.openclaw.brain-server.plist
sleep 5
SECRET=$(cat ~/.openclaw/credentials/.personal_webhook_secret)
curl -sf -H "Authorization: Bearer $SECRET" http://127.0.0.1:8791/brain/health | jq .
```

### Mode B — Docker compose (portable / fresh-machine)

For deployment on a Linux box, in CI, or in a sandbox:

```bash
git clone <brain repo> brain
cd brain
mkdir -p ~/.openclaw/credentials
echo "$(openssl rand -hex 32)" > ~/.openclaw/credentials/.personal_webhook_secret
chmod 600 ~/.openclaw/credentials/.personal_webhook_secret

docker compose up -d
docker compose ps
docker compose logs -f brain | head -40

SECRET=$(cat ~/.openclaw/credentials/.personal_webhook_secret)
curl -sf -H "Authorization: Bearer $SECRET" http://127.0.0.1:8791/brain/health | jq .
```

First-boot expectations:
- `docker compose up` builds the brain image (~3-5 min on first build, cached after)
- Qdrant starts in ~5s
- Ollama starts in ~5s but the embedder model is NOT pre-pulled — fetch it inside the container before first eval:
  ```bash
  docker exec -it brain-ollama ollama pull blaifa/multilingual-e5-large-instruct
  ```
- Neo4j starts in ~20s (waits for healthcheck)
- Brain blocks on Qdrant+Neo4j healthchecks via `depends_on`, then runs `check_and_migrate` and serves :8791
- Total cold start: ~60-90s

`BRAIN_AUTOPILOT_DISABLED=1` is set by default so the autonomous self-learning loops don't fire on a fresh box. Flip it to `0` once you've verified manual operation works.

### Mode C — Bare metal / VM (no Docker)

Same recipe as Mode A but on Linux:

```bash
sudo apt install python3.14 python3.14-venv git
git clone <brain repo> /opt/brain
cd /opt/brain
python3.14 -m venv .venv
.venv/bin/pip install -e .

# Install the three storage backends manually
# - Qdrant:    cargo install --git https://github.com/qdrant/qdrant --tag v1.17.0 --locked qdrant
#              OR on Linux: docker run -d -p 6333:6333 -v $PWD/qdrant-data:/qdrant/storage qdrant/qdrant:v1.17.0
# - Ollama:    curl -fsSL https://ollama.com/install.sh | sh
# - Neo4j:     apt install neo4j  (or download tarball)

# Bearer secret
mkdir -p $HOME/.openclaw/credentials
openssl rand -hex 32 > $HOME/.openclaw/credentials/.personal_webhook_secret
chmod 600 $HOME/.openclaw/credentials/.personal_webhook_secret

# Run brain
.venv/bin/python server.py
```

Then write a systemd unit at `/etc/systemd/system/brain.service`:

```ini
[Unit]
Description=Brain second-brain server
After=network.target

[Service]
Type=simple
User=brain
WorkingDirectory=/opt/brain
Environment="BRAIN_AUTOPILOT_DISABLED=0"
Environment="BRAIN_ATOMS_ENABLED=true"
Environment="BRAIN_ATOMS_READ=true"
ExecStart=/opt/brain/.venv/bin/python /opt/brain/server.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

`sudo systemctl enable --now brain` to start.

## Backups

- **Qdrant**: `cli/backup_qdrant.py` — runs nightly via `ai.openclaw.qdrant-backup` launchd plist (3am). Uses Qdrant's snapshot API per collection, tars all snapshots, uploads to MinIO `rag-backups/`. Independent failure domain from brain-server so a brain crash doesn't take backups with it. Also dumps knowledge tree (`raw/inbox` + `canonical` + `distilled`) and a raw JSON of `semantic_memory` as extra safety nets.
- **Neo4j**: `cli/backup_neo4j.py` — daily `neo4j-admin database dump`.
- **Brain DBs** (`brain.db`, `autonomy.db`): SQLite WAL files. Backup script copies via `sqlite3 .backup` (atomic snapshot, no downtime).
- **Restore**: stop brain → `qdrant-client` snapshot restore API (`PUT /collections/{name}/snapshots/upload`) → restart brain.

Backup verification is automated: `cli/backup_verify.py` runs monthly (1st of month, 4:30am) — extracts the latest backup into a sandbox, loads the snapshot into a temp Qdrant instance, runs a smoke query, asserts non-empty.

## Health monitoring

- **Self-monitoring**: `/brain/health` returns `{status: "healthy"|"degraded"|"down", services: {qdrant, ollama, neo4j}, alerts: [...]}`. SLOs check every 5 min.
- **External**: Add `https://brain.chrischodev.com/healthz` to Uptime Kuma. The `/healthz` route is unauth and returns `{status: "ok"}` if the FastAPI server is up; it doesn't probe storage backends — use `/brain/health` for full status.
- **Telegram alerts**: SLO breaches use `brain_core/telegram_alert.py` direct Telegram Bot API delivery with backlog replay. Deterministic remediation runs first for safe mechanical fixes; OpenClaw is not required for Chris-facing alert delivery.

## Upgrade path

```bash
cd /opt/brain
git pull
.venv/bin/pip install -e .          # picks up new deps
.venv/bin/python cli/brain_init.py migrate  # runs schema migrations
launchctl kickstart -k gui/$UID/ai.openclaw.brain-server  # restart
sleep 5
curl -sf -H "Authorization: Bearer $SECRET" http://127.0.0.1:8791/brain/health
```

Schema migrations are idempotent (`CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ... IF NOT EXISTS column` patterns) so re-running `brain_init.py migrate` on an already-up-to-date schema is a no-op.

## Multi-machine / cloud (NOT supported today)

Brain is single-tenant single-user by design. Multi-tenancy would require:
- Per-tenant bearer tokens + RBAC
- Per-tenant Qdrant collections
- Per-tenant atoms tables
- Per-tenant rate limit buckets

None of this is implemented. Treat the brain as a personal device, not a SaaS.

## Pre-flight checklist (fresh machine)

1. ✅ Python 3.14 installed
2. ✅ Qdrant + Ollama + Neo4j running and reachable on localhost
3. ✅ Bearer secret at `~/.openclaw/credentials/.personal_webhook_secret` (chmod 600)
4. ✅ Embed model pulled in Ollama: `ollama pull blaifa/multilingual-e5-large-instruct`
5. ✅ Brain venv installed: `pip install -e .` from project root
6. ✅ `cli/brain_init.py check` reports all components healthy
7. ✅ Brain reachable: `curl -fsS http://127.0.0.1:8791/healthz`
8. ✅ Eval baseline captured: `cli/eval_compare.py --json --eval-set cli/eval_set_stable.json`

If all 8 pass, the brain is operational.
