# OpenClaw Archive — 2026-05-23

Final selective archive at the OpenClaw → Hermes migration cutover.

## Contents

- `personas/<name>/` — Per-persona OpenClaw configs (AGENTS.md, SOUL.md, SCRATCH.md, learnings/)
- `configs/openclaw.json` — Main OpenClaw config (channels, models, mcp servers, accounts)
- `configs/.env.legacy` — Env vars at cutover (Telegram tokens, Cloudflare, Tavily, etc.)
  Note: This is a SNAPSHOT. Live env now lives at ~/.brain/.env.

## Where to look for more

Full archive of ~/.openclaw at the cutover time:
`~/Archives/openclaw-final-2026-05-23.tar.gz`

That tarball contains EVERYTHING from ~/.openclaw — sessions, skills, workspace
state, tmp, settings, telegram pairing, subagents, etc.

## Why archived (not deleted entirely)

Per Chris's 2026-05-23 directive: OpenClaw cleanup committed, no plan to revert.
Important info preserved for future reference (persona definitions, learnings,
session history) so brain canonical can be re-derived if needed.
