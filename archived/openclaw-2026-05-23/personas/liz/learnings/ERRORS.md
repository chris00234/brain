## [ERR-20260227-001] acp-claude-agent-to-agent-retrieval

**Logged**: 2026-02-28T04:40:00Z
**Priority**: high
**Status**: pending
**Area**: config

### Summary
ACP Claude run was accepted, but result retrieval failed due to missing/invalid external API key in downstream ACP session.

### Error
```
Internal error: Invalid API key · Fix external API key
```

### Context
- Operation: sessions_send to `agent:claude:acp:abea6c35-5f78-48e8-adea-e830531e00df`
- Earlier blocker resolved: `tools.agentToAgent.allow` permissions were adjusted.
- New blocker: provider/API key config for Claude ACP backend appears invalid.

### Suggested Fix
- Verify ACP adapter/provider key used by Claude harness is configured and valid.
- Re-run a minimal one-shot ACP task to confirm end-to-end success before larger jobs.

### Metadata
- Reproducible: unknown
- Related Files: N/A

---
