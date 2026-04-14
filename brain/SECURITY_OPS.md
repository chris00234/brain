# Brain — Security Operations

Operational security procedures for the brain's bearer-token auth model. This is the **single-user / single-token** posture; multi-tenant + RBAC is out of scope (see COMMERCIAL_READINESS.md).

## Bearer token rotation

The brain uses a single bearer token for all authenticated routes. Token lives at `~/.openclaw/credentials/.personal_webhook_secret` (chmod 600). Rotation is a 5-step procedure that takes ~2 minutes.

### When to rotate

- **Routine**: every 90 days, on the 1st of the quarter
- **Compromise**: immediately if you suspect the token leaked (clipboard paste, screen share, log dump, lost laptop)
- **Personnel change**: every time a contractor or non-Chris user gets read access to `~/.openclaw/credentials/`
- **After audit**: every time you run `/cso` or similar security audit

### Rotation procedure

```bash
# 1. Generate the new token
NEW_SECRET=$(openssl rand -hex 32)

# 2. Backup the current token (in case rollback is needed mid-rotation)
cp ~/.openclaw/credentials/.personal_webhook_secret \
   ~/.openclaw/credentials/.personal_webhook_secret.prev-$(date +%Y%m%d-%H%M%S)
chmod 600 ~/.openclaw/credentials/.personal_webhook_secret.prev-*

# 3. Atomically swap to the new token
echo "$NEW_SECRET" > ~/.openclaw/credentials/.personal_webhook_secret.new
chmod 600 ~/.openclaw/credentials/.personal_webhook_secret.new
mv ~/.openclaw/credentials/.personal_webhook_secret.new \
   ~/.openclaw/credentials/.personal_webhook_secret

# 4. Restart brain-server to pick up the new token from disk
launchctl kickstart -k gui/$(id -u)/ai.openclaw.brain-server
sleep 5

# 5. Verify the new token works
NEW=$(cat ~/.openclaw/credentials/.personal_webhook_secret)
curl -sf -H "Authorization: Bearer $NEW" http://127.0.0.1:8791/brain/health | jq -r '.status'

# Expected: "healthy" or "degraded" (with non-empty alerts list)
# If you get 401: rollback via `mv .personal_webhook_secret.prev-* .personal_webhook_secret`
```

### Side effects of rotation

- **Per-bearer rate limit buckets reset** — the bearer-keyed slowapi limiter (M7-WS7) hashes the token to derive the bucket key. After rotation, the new token starts with a fresh quota.
- **MCP servers continue working** — `brain_mcp_server.py` re-reads the secret file on every request via the global `SECRET` variable initialized at module load. Restart the MCP server to pick up the new token. For Claude Code: restart Claude Code. For OpenClaw agents: `launchctl kickstart -k gui/$UID/ai.openclaw.gateway`.
- **Cron jobs re-read the secret** — every job in `JOB_REGISTRY` that needs the secret reads it from disk via `cat`, so they pick up the new token on next fire.
- **Action_audit lineage** — the `actor` column doesn't track which token signed the request, only the `x-agent` header. So rotating the token doesn't break per-actor analytics.

## Token storage hygiene

- **Never log it**: brain's structured logger has a `_log_safe` wrapper; `Authorization` headers are scrubbed before write. Verify by `grep -r 'Bearer' logs/ | head` — should never return your actual token.
- **Never commit it**: `.personal_webhook_secret` is in `.gitignore` everywhere. Pre-commit hook should fail any commit that contains a 64-char hex string in a non-credentials path.
- **Never paste into pastebins / Slack / clipboards**: if you do, treat it as compromised and rotate immediately.
- **Backups**: the bearer secret IS backed up via standard macOS Time Machine. If your TM volume is encrypted (it should be — `sudo fdesetup status`), this is acceptable.

## Quiet hours + denylist (defense in depth)

The brain has two operational gates that complement the rate limiter:

- **Quiet hours**: 23:00–07:00 PT by default. During this window, all autonomous actions are demoted to L0 (advisory only — no writes, no LLM dispatches without explicit approval). Configure: `POST /brain/quiet-hours` with `{"start": "23:00", "end": "07:00"}`.
- **Soft denylist**: per-action prefix denylist for autonomous actions. Add via `POST /brain/denylist/add` with `{"prefix": "rm -rf"}`. The autonomy gate refuses any matching action.

Both are queryable via `GET /brain/quiet-hours` and `GET /brain/denylist`.

## Top kill switch

If something goes wrong and you need to stop all autonomous brain activity in <5 seconds:

```bash
launchctl setenv BRAIN_AUTOPILOT_DISABLED 1
launchctl kickstart -k gui/$(id -u)/ai.openclaw.brain-server
```

Every `autonomy.authorize()` call returns L0 after this. To re-enable:

```bash
launchctl unsetenv BRAIN_AUTOPILOT_DISABLED
launchctl kickstart -k gui/$(id -u)/ai.openclaw.brain-server
```

## Incident response checklist

If you suspect a token leak or unauthorized access:

1. **Rotate the token immediately** (see procedure above)
2. **Set `BRAIN_AUTOPILOT_DISABLED=1`** to freeze autonomous actions
3. **Audit recent activity**: `curl -sf -H "Authorization: Bearer $NEW" http://127.0.0.1:8791/brain/usage?days=7 | jq` — look for actors / tools you don't recognize
4. **Inspect `action_audit`**: `sqlite3 logs/brain.db 'SELECT actor, tool, query_text, created_at FROM action_audit WHERE created_at > datetime("now", "-24 hours") ORDER BY id DESC LIMIT 50;'`
5. **Check `/brain/breakers`**: any tripped breakers might indicate an attack pattern
6. **Pull error logs**: `tail -200 logs/server.log; tail -200 logs/failures.jsonl`
7. **Decide**: legitimate misuse (your own script gone wild) vs unauthorized (someone else has the token)
8. **If unauthorized**: rotate cloudflare access policy, audit nginx access logs, check for ssh-key compromise

## SOC2-track items (deferred)

The following items would be required for a SOC2 Type 2 audit but are NOT implemented today. They are documented here for awareness only:

- **Key rotation automation** (e.g., HashiCorp Vault, AWS Secrets Manager)
- **Audit log immutability** (write-once, append-only, off-host)
- **Pen test report** (annual external)
- **Incident postmortem template** (15-min, root cause, action items)
- **Vendor security review** (every dependency upgrade)
- **CVE scanning in CI** (e.g., Trivy, Snyk)
- **Encryption at rest** for SQLite WAL files
- **Time-bounded session tokens** (replacing the static bearer)

These would close gap #3 (Security posture) in COMMERCIAL_READINESS.md from 3 → 5.
