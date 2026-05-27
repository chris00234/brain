# OpenClaw Multi-Agent Setup Documentation

**Author**: Chris Cho
**Date**: 2026-02-24
**OpenClaw Version**: 2026.2.22 (update to 2026.2.24 recommended)
**Server**: Ubuntu 24.04 LTS, 4 cores, 15GB RAM, 468GB disk

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Agent Team](#2-agent-team)
3. [Telegram Bot Setup](#3-telegram-bot-setup)
4. [War Room Group Chat](#4-war-room-group-chat)
5. [Agent-to-Agent Communication](#5-agent-to-agent-communication)
6. [Workspace Files](#6-workspace-files)
7. [Native Cron Jobs](#7-native-cron-jobs)
8. [System Cron Jobs](#8-system-cron-jobs)
9. [Optimizations Applied](#9-optimizations-applied)
10. [Skills Installed](#10-skills-installed)
11. [Webhook Configuration](#11-webhook-configuration)
12. [ClawMetry Observability](#12-clawmetry-observability)
13. [Config File Reference](#13-config-file-reference)
14. [Troubleshooting Log](#14-troubleshooting-log)
15. [Pending / Needs Update](#15-pending--needs-update)

---

## 1. Architecture Overview

```
Chris (Telegram DM)
├── Jenna Bot  ─→  Jenna Agent (personal life manager)
├── Liz Bot    ─→  Liz Agent   (principal engineer)
└── Ellie Bot  ─→  Ellie Agent (AI & infra specialist)

War Room Group (-5200401184)
├── All 3 bots present
├── requireMention: false
└── Agents collaborate on cross-domain tasks

Agent-to-Agent (backend)
├── sessions_send with full session keys
├── maxPingPongTurns: 5
└── sessions visibility: all

External Triggers
├── /hooks/github    → Liz
├── /hooks/server-alert → Ellie
├── /hooks/generic   → Jenna
└── /hooks/wake (gmail preset)
```

**Routing Strategy (Option B)**:
- **DM each agent directly** (90% of usage, cheap)
- **War Room group** for live collaboration (10%, expensive)
- **Redirect**: For complex cross-domain requests, agents redirect Chris to the right teammate
- **Silent consult**: For quick yes/no questions, agents use `sessions_send` behind the scenes

---

## 2. Agent Team

### Jenna - Chief of Staff / Personal Life Manager

| Field | Value |
|-------|-------|
| Agent ID | `jenna` |
| Workspace | `/home/chris/.openclaw/workspace-jenna` |
| Telegram Bot | `@jenna_bot` (token: `8102904360:AAFz...`) |
| Heartbeat | Every 30 min, active 07:00-23:00 |
| Model | gpt-5.3-codex (fallback: claude-opus-4-6) |

**Responsibilities**:
- Morning briefing (7 AM), evening wrap-up (6 PM)
- Calendar, scheduling, meeting reminders
- Email triage and drafting
- Daily planning with Todoist integration
- Personal tasks, shopping lists, travel
- Team coordination (pulls status from Liz & Ellie)

**Cron Jobs Owned**:
- Morning briefing (7 AM)
- Daily Plan / Todoist (7 AM)
- Midday Replan / Todoist (1:05 PM)
- Evening Wrap / Todoist (5 PM)
- Weekly Review / Todoist (Sun 6:30 PM)

### Liz - Principal Staff Engineer (30yr Google veteran persona)

| Field | Value |
|-------|-------|
| Agent ID | `liz` |
| Workspace | `/home/chris/.openclaw/workspace-liz` |
| Telegram Bot | `@liz_bot` (token: `8685578584:AAEW...`) |
| Heartbeat | None (on-demand only) |
| Model | gpt-5.3-codex (fallback: claude-opus-4-6) |

**Responsibilities**:
- Code review with rigor (correctness, security, patterns)
- Architecture & design (trade-off analysis)
- Systematic debugging (reproduce → isolate → root cause → fix)
- Pair programming and technical mentorship
- AI engineering evaluation (when Ellie finds tools)

**Personality traits**:
- Direct, precise, opinionated with reasoning
- Values simplicity, correctness, consistency, tests
- "This has a SQL injection vulnerability" not "you might want to consider..."

### Ellie - AI & Infrastructure Specialist

| Field | Value |
|-------|-------|
| Agent ID | `ellie` |
| Workspace | `/home/chris/.openclaw/workspace-ellie` |
| Telegram Bot | `@ellie_bot` (token: `8751034788:AAGq...`) |
| Heartbeat | Every 6 hours, active 24/7 |
| Model | gpt-5.3-codex (fallback: claude-opus-4-6) |

**Responsibilities**:
- Docker container management (27 containers)
- Full deployment pipeline (Docker → Nginx → Cloudflare → Homer)
- Server health monitoring (CPU, RAM, disk, containers)
- AI research, experiments, benchmarking
- Automation building (cron, scripts, integrations)
- Security monitoring

**Cron Jobs Owned**:
- Automation Health Check (every 1h)
- Security Guardrail Check (every 12h)
- Weekly Cron Observability Report (Sun 6 PM)
- Obsidian Organizer (Sun 8 PM)

---

## 3. Telegram Bot Setup

### Bot Creation (via @BotFather)
1. Created 3 bots: Jenna, Liz, Ellie
2. For each bot:
   - `/setjoingroups` → **Enable** (required to add to groups)
   - `/setprivacy` → **Disable** (required to see all group messages)
   - Send `/start` to each bot in DM before first use

### Bot Tokens
```
jenna-bot: 8102904360:AAFzQMSzaIMJug78F25nZM__K31npDHSkmA
liz-bot:   8685578584:AAEW2liz2YrU1YUljd8iWxRg1HDjYQy_E9Y
ellie-bot: 8751034788:AAGqajzl06vcO32LJcC9jr-EtW8scCFHS9w
```

### Telegram Config
- **DM Policy**: allowlist (`8484060831` Chris, `8070618093` wife Jenna)
- **Group Policy**: allowlist (War Room is exception with open policy)
- **Streaming**: partial (live preview updates)

---

## 4. War Room Group Chat

| Field | Value |
|-------|-------|
| Group Name | War Room |
| Chat ID | `-5200401184` |
| Policy | open (anyone in group can talk) |
| requireMention | false (bots see all messages) |

### Group Chat Rules (in all AGENTS.md)
- Only respond if Chris @mentions you OR another agent @mentions you
- If not addressed, reply with `REPLY_SKIP` to stay silent
- Keep responses concise in group
- NEVER respond to your own messages or create loops

### How to get group ID
Used `@getmyid_bot` (added to group temporarily) since OpenClaw consumes Telegram updates via polling, making `getUpdates` API return empty.

---

## 5. Agent-to-Agent Communication

### Configuration (openclaw.json)
```json
"tools": {
  "agentToAgent": {
    "enabled": true,
    "allow": ["jenna", "liz", "ellie"]
  },
  "sessions": {
    "visibility": "all"
  }
},
"session": {
  "agentToAgent": {
    "maxPingPongTurns": 5
  }
}
```

### Session Keys (CRITICAL - must use full format)
```
Jenna: agent:jenna:telegram:direct:8484060831
Liz:   agent:liz:telegram:direct:8484060831
Ellie: agent:ellie:telegram:direct:8484060831
```

Using just `"ellie"` as session label will fail with "No session found."

### Communication Methods
1. **sessions_send**: Direct message another agent's session (supports reply-back loop)
2. **sessions_list**: Discover active agent sessions
3. **sessions_history**: Read transcripts from other sessions
4. **sessions_spawn**: Spawn sub-agent tasks

---

## 6. Workspace Files

### Structure (per agent)
```
workspace-{agent}/
├── SOUL.md       # Personality, voice, values
├── AGENTS.md     # Operating instructions, routing, cron jobs
├── USER.md       # Chris's profile (customized per agent's needs)
├── HEARTBEAT.md  # Periodic check tasks
├── IDENTITY.md   # Agent identity metadata (auto-generated)
├── TOOLS.md      # Tool documentation (auto-generated)
└── memory/       # Daily notes, long-term memory
```

### File Locations
```
Jenna: /home/chris/.openclaw/workspace-jenna/
Liz:   /home/chris/.openclaw/workspace-liz/
Ellie: /home/chris/.openclaw/workspace-ellie/
Main:  /home/chris/.openclaw/workspace/ (old single-agent, still used by cron scripts)
```

### USER.md Customization
Each agent's USER.md contains different info:
- **Jenna**: Wife info, daily schedule, investments, morning briefing format
- **Liz**: Tech preferences (React, FastAPI, Vercel), active projects, coding standards
- **Ellie**: Full server specs, 27-service table, all ports/domains, deployment checklist, config paths, SSH config

---

## 7. Native Cron Jobs

All reassigned from `main` to proper agents:

### Jenna's Jobs
| Name | Schedule | What it does |
|------|----------|-------------|
| 아침 브리핑 (7 AM) | `0 7 * * *` PST | Runs `morning_briefing.py telegram` + `daily_surprise.py`, sends to Chris & wife |
| Daily Plan (Todoist) | `0 7 * * *` PST | Runs `calendar_todoist_planner.py morning` |
| Midday Replan (Todoist) | `5 13 * * *` PST | Runs `calendar_todoist_planner.py midday` |
| Evening Wrap (Todoist) | `0 17 * * *` PST | Runs `calendar_todoist_planner.py evening` |
| Weekly Review (Todoist) | `30 18 * * 0` PST | Runs `calendar_todoist_planner.py weekly` |

### Ellie's Jobs
| Name | Schedule | What it does |
|------|----------|-------------|
| Automation Health Check | Every 1h | Runs `cron_observability_report.py issues 2` |
| Security Guardrail Check | Every 12h | Runs `security_guardrail_check.py` |
| Weekly Cron Observability | `0 18 * * 0` PST | Runs `cron_observability_report.py report 168` |
| Obsidian Organizer | `0 20 * * 0` PST | Runs `obsidian_sync.py pull` + `list` |

### Managing Cron
```bash
openclaw cron list                    # View all jobs
openclaw cron edit <id> --agent ellie # Reassign to agent
openclaw cron edit <id> --disable     # Disable job
openclaw cron run <id>                # Run now (debug)
openclaw cron rm <id>                 # Delete job
```

---

## 8. System Cron Jobs

Only one remains in system crontab (duplicate morning briefing was removed):

```crontab
*/5 * * * * /home/chris/.openclaw/workspace/scripts/email_fallback_runner.sh
```

This runs `email_monitor_lite.py` which checks Gmail IMAP every 5 min for important emails and alerts via Telegram. No LLM tokens used.

### Key Scripts Still Active
| Script | Purpose |
|--------|---------|
| `morning_briefing.py` | Weather, stock analysis (RSI, MACD, Bollinger, VIX), wife email |
| `email_monitor_lite.py` | Gmail IMAP filter → Telegram alerts |
| `email_fallback_runner.sh` | Lock-safe wrapper for email monitor |
| `cron_observability_report.py` | Cron health reporting |
| `security_guardrail_check.py` | Security posture checks |
| `calendar_todoist_planner.py` | Todoist + Google Calendar planner |
| `obsidian_sync.py` | Obsidian vault CouchDB sync |
| `daily_surprise.py` | Daily fun content |

---

## 9. Optimizations Applied

### Vector Memory Search
```json
"memorySearch": {
  "provider": "local",
  "query": {
    "hybrid": {
      "enabled": true,
      "vectorWeight": 0.7,
      "textWeight": 0.3,
      "mmr": { "enabled": true, "lambda": 0.7 },
      "temporalDecay": { "enabled": true, "halfLifeDays": 30 }
    }
  }
}
```
- **Hybrid search**: Combines BM25 keyword matching (30%) with vector semantic matching (70%)
- **MMR re-ranking**: Eliminates redundant results (lambda 0.7)
- **Temporal decay**: Recent memories ranked higher (30-day half-life, MEMORY.md never decays)

### Active Hours for Heartbeat
- **Default (Jenna, Liz)**: 07:00-23:00 — no heartbeat burns outside waking hours
- **Ellie override**: 00:00-23:59 — server monitoring needs 24/7 coverage

### Sub-Agent Configuration
```json
"subagents": {
  "maxConcurrent": 8,
  "maxSpawnDepth": 2,
  "maxChildrenPerAgent": 5,
  "model": "openai-codex/gpt-5.3-codex",
  "thinking": "medium"
}
```
- Depth 2 enables orchestrator pattern: Main → Orchestrator → Workers
- Each agent can spawn up to 5 children
- Sub-agents use gpt-5.3-codex with medium thinking level

### Model Configuration
- **Primary**: `openai-codex/gpt-5.3-codex`
- **Fallback**: `anthropic/claude-opus-4-6`
- **Available**: Claude Sonnet 4.5 (aliased as `sonnet`)
- All models configured with `cost: 0` (included in API plan)

### LLM Task Plugin
```json
"plugins": { "entries": { "llm-task": { "enabled": true } } }
```
Enables structured LLM steps within Lobster workflow pipelines.

---

## 10. Skills Installed

### Shared Skills (`~/.openclaw/skills/`)
| Skill | Source | Purpose |
|-------|--------|---------|
| leonardo-ai | Managed | Image generation via Leonardo AI API |
| tesla | Managed | Tesla vehicle control (lock, climate, trunk, etc.) |
| todoist | ClawHub | Direct Todoist task management |
| lobster | ClawHub | Deterministic workflow runtime with approval gates |

### Bundled Skills (Ready)
| Skill | Purpose |
|-------|---------|
| clawhub | Search, install, update skills from ClawHub marketplace |
| coding-agent | Delegate coding tasks to sub-agents |
| discord | Discord operations via message tool |
| gh-issues | GitHub issues → sub-agent implementation |
| github | GitHub CLI operations (PRs, CI, code) |
| gog | Google Workspace CLI (Gmail, Calendar, Drive, Contacts) |
| healthcheck | Host security hardening |
| nano-banana-pro | Image generation/editing via Gemini 3 |
| obsidian | Obsidian vault operations |
| weather | Weather via wttr.in or Open-Meteo |

### Skill Config
```json
"skills": {
  "load": { "extraDirs": ["/home/chris/.openclaw/skills"] },
  "entries": {
    "todoist": { "enabled": true, "env": { "TODOIST_API_TOKEN": "5c14eb..." } },
    "lobster": { "enabled": true },
    "tavily": { "enabled": true, "env": { "TAVILY_API_KEY": "tvly-dev-..." } }
  }
}
```

### ClawHub Account
- **Username**: `@chris00234` (GitHub OAuth)
- **CLI**: `clawhub` installed at `/home/chris/.nvm/versions/node/v22.22.0/bin/clawhub`
- **Token**: `clh_07DW46L0HyxPZmOdzwG--Yx22ILILiU6xcHIBS0t940`

---

## 11. Webhook Configuration

```json
"hooks": {
  "enabled": true,
  "path": "/hooks",
  "token": "960cf689770f361d6aab37efae5d3219da66f5f72637d508",
  "presets": ["gmail"],
  "mappings": [
    { "name": "github",       "agentId": "liz" },
    { "name": "server-alert", "agentId": "ellie" },
    { "name": "generic",      "agentId": "jenna" }
  ]
}
```

### Webhook Endpoints
```
POST http://127.0.0.1:18789/hooks/wake          # System wake event
POST http://127.0.0.1:18789/hooks/github         # → Liz (PR, push, CI events)
POST http://127.0.0.1:18789/hooks/server-alert   # → Ellie (Uptime Kuma, Grafana alerts)
POST http://127.0.0.1:18789/hooks/generic        # → Jenna (catch-all)
```

### Authentication
Include header: `Authorization: Bearer 960cf689770f361d6aab37efae5d3219da66f5f72637d508`

### Usage with Uptime Kuma (example)
In Uptime Kuma notification settings, add webhook:
```
URL: http://127.0.0.1:18789/hooks/server-alert
Method: POST
Header: Authorization: Bearer 960cf689...
```

---

## 12. ClawMetry Observability

### Installation
```bash
pip install --break-system-packages clawmetry
```

### Version
`clawmetry 0.9.17`

### Usage
```bash
clawmetry                    # Launch dashboard (default port)
clawmetry --port 8095        # Custom port
```

### What it shows
- Real-time token costs per agent
- Sub-agent activity tracking
- Cron job execution history
- Memory changes over time
- Session history and duration

---

## 13. Config File Reference

### Main Config
`~/.openclaw/openclaw.json` — all settings in one file

### Agent Workspaces
```
~/.openclaw/workspace-jenna/   (Jenna's workspace)
~/.openclaw/workspace-liz/     (Liz's workspace)
~/.openclaw/workspace-ellie/   (Ellie's workspace)
~/.openclaw/workspace/         (old main agent, still used by cron scripts)
```

### Skills
```
~/.openclaw/skills/            (shared across all agents)
~/.openclaw/workspace/skills/  (old workspace skills — also loaded)
```

### Sessions & Memory
```
~/.openclaw/agents/main/       (main agent sessions, 17M SQLite)
~/.openclaw/agents/jenna/      (jenna sessions)
~/.openclaw/agents/liz/        (liz sessions)
~/.openclaw/agents/ellie/      (ellie sessions)
~/.openclaw/memory/            (vector memory SQLite indexes)
```

### Credentials
```
~/.openclaw/workspace/.env                       (Gmail, Ghost, Telegram, Discord, GitHub)
~/.openclaw/workspace/memory/credentials.md      (plaintext reference — handle with care)
~/.openclaw/workspace/client_secret_google_calendar.json  (OAuth secret)
~/.openclaw/workspace/google_calendar_token.json  (OAuth refresh token)
```

### Infrastructure Configs (server)
```
/etc/nginx/sites-available/chrischodev           (Nginx)
/etc/cloudflared/config.yml                       (Cloudflare Tunnel)
/home/chris/homer/assets/config.yml               (Homer dashboard)
/home/chris/monitoring/prometheus/prometheus.yml   (Prometheus)
```

---

## 14. Troubleshooting Log

Issues encountered during setup and their fixes:

### `agentId` vs `id` in agents.list
- **Error**: Config validation failed
- **Fix**: Use `"id"` in `agents.list[]` entries, `"agentId"` in `bindings[]`

### `session.agentToAgent` wrong location
- **Error**: `enabled` and `allowList` keys not recognized under `session`
- **Fix**: Move to `tools.agentToAgent` with keys `enabled` and `allow` (not `allowList`)

### Bots can't be invited to Telegram group
- **Fix**: `/setjoingroups` → Enable via @BotFather, then restart OpenClaw so bots go online

### `getUpdates` returns empty
- **Cause**: OpenClaw polling consumes all updates
- **Fix**: Use `@getmyid_bot` in group instead

### `sessions_send` fails: "No session found with label: ellie"
- **Cause**: Using short name instead of full session key
- **Fix**: Use `agent:ellie:telegram:direct:8484060831` format

### Cross-agent messaging blocked: "sessions visibility is restricted"
- **Cause**: Default visibility is `"tree"` (own sessions only)
- **Fix**: Add `"sessions": {"visibility": "all"}` to `tools`

### War Room bots not responding to each other
- **Cause**: `requireMention: true` meant bots only respond to @mentions
- **Fix**: Set `requireMention: false` for War Room group

### Conversation ends after one iteration in War Room
- **Cause**: Missing `maxPingPongTurns`
- **Fix**: Add `session.agentToAgent.maxPingPongTurns: 5`

### `memorySearch` config warning
- **Error**: "top-level memorySearch was moved"
- **Fix**: Move `memorySearch` under `agents.defaults.memorySearch`

### `promptCaching` unrecognized
- **Cause**: Feature added in v2026.2.23, running v2026.2.22
- **Fix**: Removed from config, will re-add after updating OpenClaw

### ClawHub rate limiting
- **Cause**: Too many API calls in short period
- **Fix**: Wait 30-60 seconds between requests, or use `--force` flag

---

## 15. Pending / Needs Update

### Requires OpenClaw v2026.2.24
Run `openclaw update` to get these features:

| Feature | What it enables |
|---------|----------------|
| Prompt Caching | Per-agent cache tuning for SOUL.md/AGENTS.md to reduce repeat costs |
| Canvas/A2UI | Agent-driven visual dashboards on port 18793 |
| Kilo Gateway | Additional model provider |
| Session Maintenance | Disk-budget controls for session storage |

### After updating, add to openclaw.json:
```json
// Under agents.defaults:
"promptCaching": {
  "enabled": true,
  "scope": "agent"
}

// Top-level:
"canvas": {
  "enabled": true,
  "port": 18793
}
```

### Other Opportunities
- **Gmail Pub/Sub**: Replace `email_monitor_lite.py` polling with real-time push
- **Security Audit**: Run `openclaw security audit --deep` to harden
- **Model Failover**: Current fallback chain works; consider adding Sonnet as middle fallback
- **Per-Agent Sandbox**: Lock down Jenna (no filesystem), give Ellie full exec

---

## Cleanup Done

### Files Deleted
- 6 duplicate Gmail scripts (`gmail_monitor.py`, `gmail_reader.py`, `gmail_checker.py`, `gmail_imap_check.py`, `check_gmail.py`, `email_monitor.py`)
- 3 Twitter scripts + env (`twitter_bot.py`, `twitter_browser_bot.py`, `twitter_poster.py`, `.env.twitter`, `.env.twitter.template`)
- 4 `BOOTSTRAP.md` files (all workspaces)

### Duplicate System Crontab Removed
- `morning_briefing.py` was running both as system crontab AND native OpenClaw cron at 7AM
- Removed system crontab entry; native cron (assigned to Jenna) handles it

### Files Kept
- `credentials.md` in workspace/memory/ (per user request)
- `email_monitor_lite.py` + `email_fallback_runner.sh` (active in system crontab)
- All scripts in workspace/scripts/ (active utilities)
- `brain-control/` project (active development)
