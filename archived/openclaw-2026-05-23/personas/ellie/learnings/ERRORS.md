# ERRORS

## [ERR-20260227-001] acpx/claude non-interactive permission failure

**Logged**: 2026-02-27T14:39:00-08:00
**Priority**: high
**Status**: pending
**Area**: infra

### Summary
Claude ACP/acpx coding run failed when write/edit permissions were required in a non-interactive execution context.

### Error
```
Permission prompt unavailable in non-interactive mode
```

### Context
- Operation attempted: run Claude coding task via acpx/ACP from OpenClaw automation flow.
- Effective policy at failure time:
  - `permissionMode: approve-reads`
  - `nonInteractivePermissions: fail`
- Impact:
  - read-only smoke checks can pass,
  - real coding runs requiring file writes fail.

### Suggested Fix
- For autonomous coding runs, switch to a non-blocking write policy in this execution path:
  - `permissionMode: approve-all`
- Or run in interactive mode where permission prompts are available.
- Keep safety by limiting scope (`cwd`), using verify gates, and requiring explicit deploy confirmation.

### Metadata
- Reproducible: yes
- Related Files: /Users/chrischo/.openclaw/openclaw.json
- See Also: ACP run errors around runId `7ec5b25f-f7ae-43c9-9bb59a068e75`

---

## [ERR-20260228-002] jenna tool-policy/session-path regression during Tesla incident

**Logged**: 2026-02-28T20:35:00-08:00
**Priority**: critical
**Status**: pending
**Area**: infra

### Summary
Jenna Tesla 실행 경로 복구 중 tool policy와 live session 정리 작업이 겹치며 사용자 채널에 혼선 응답이 노출됨.

### Error
```
- Jenna responses oscillated between "tool access unavailable" and pseudo-forwarded status text.
- lane wait spikes observed (up to ~602s), causing stale/misaligned replies.
```

### Context
- Operation attempted: Jenna Tesla command-path recovery.
- Risky actions performed during active user conversation:
  - direct session key cleanup/reset
  - additive permissions set via `tools.allow` instead of `tools.alsoAllow`
- Impact:
  - user-facing reliability drop
  - trust impact from inconsistent answers

### Suggested Fix
- Enforce runbook:
  - no active direct-session reset without explicit maintenance consent
  - additive policy changes via `alsoAllow` only
  - mandatory post-change canary before user handoff
- Keep Ellie as temporary execution fallback until Jenna canary passes.

### Metadata
- Reproducible: yes
- Related Files: /Users/chrischo/.openclaw/openclaw.json, /Users/chrischo/.openclaw/agents/jenna/sessions/sessions.json
- See Also: ERR-20260227-001, LRN-20260228-002

---
