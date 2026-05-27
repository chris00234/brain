# Next sprint — 2026-04-23 Chris approved

Five items. Agency/embodiment Phase 2 + long-overdue cleanups.

## 1. Canonical staleness detector

Core problem (directly observed this session): brain flagged `search.py` missing `argparse` import as 10/10 severity across 5 redundant canonical chunks. The bug was already fixed — `import argparse` lives at line 21. The canonicals are stale. Deleting the Qdrant atoms didn't help; they regenerate from on-disk distilled files.

Goal: stop surfacing canonical claims that reality has already invalidated.

Approach:
- New job `canonical_staleness_check` (daily 4:30am, after canonical_pipeline's 2am run).
- For each canonical atom with a code reference (path + line range / symbol), verify the claim still holds:
  - "file X is missing import Y" → grep the file; if Y is present, mark obsolete.
  - "function F crashes on input Z" → best-effort execute or re-read the function body to check for the flagged pattern.
- Write `obsolete=true` to the atom's metadata + update the on-disk distilled file with a `## OBSOLETE` header, or move to `knowledge/obsolete/`.
- Search should downweight atoms marked obsolete by 10x (not just hide — keep auditable).

Why this is #1: it's the direct root cause of "brain coding-domain recall accuracy 0%".

## 2. `brain_core/speak.py` split

762 lines, violates the <300 rule. Split into:
- `speak/drives.py` — contradiction, coding_revert, stale_thread, synthesis (~350 lines)
- `speak/composer.py` — collect_observations, compose_digest, format_telegram, run_digest (~150 lines)
- `speak/urgent.py` — active_session_ids, urgent_scan, doorbell write (~100 lines)
- `speak/schema.py` — Observation dataclass + DDL + ensure_schema + _log_emit + _was_sent_recently (~100 lines)
- `speak/__init__.py` — re-exports for backwards compat so server.py + job_definitions call sites don't change

Also: break out `synthesis_drive` (~100 lines) into `_build_synthesis_prompt`, `_parse_synthesis_response`, `_dispatch_synthesis_command`.

## 3. `brain_loop` signal-driven refactor (Phase B)

Current: tick-based loop; urgent_scan fires every 5 min whether anything new happened or not.

Target: event-triggered drives.
- New abstraction: DriveTrigger with options {cron, on_table_insert, on_slo_breach, on_file_change}
- contradiction_drive triggered by `attention_queue INSERT` (SQLite trigger → brain webhook)
- coding_revert_drive triggered by `coding_event_outcomes INSERT with outcome='reverted'`
- synthesis_drive triggered by composite: "N new signals since last run OR quiet for M minutes"
- Falls back to legacy tick schedule if no triggers fired in last hour

## 4. Override authority — PreToolUse `permissionDecision=deny`

Current: PreToolUse hook injects hints; agent can ignore.
Target: deny + reason message when agent is about to touch canonical-marked-dangerous paths.

Scope (phase 1, narrow):
- `~/.brain/credentials/`
- `~/.hermes/profiles/*/config.yaml`
- `~/server/brain/models/adapters/lora_active/**`
- `~/.claude/settings.json` — only deny edits that REMOVE brain hooks (allow additions)

Contract: `brain_enforce` tag in canonical with structured policy (scope, reason, escape hatch).
Escape hatch: env var `BRAIN_OVERRIDE=1` or explicit Chris confirmation.

## 5. Self-eval loop

Current: brain has 18 SLOs but none measure "am I returning wrong things?".
Today's session found 9 bugs that had lived for weeks without SLO flagging them.

Target: `self_eval_drive` that runs nightly:
- Sample N=50 recent `/recall/v2` answers from audit log.
- For each: re-run the query, check top-3 hit overlap with the original.
- If overlap drops below 70% for >10% of samples, flag as drift.
- Additionally: run a held-out eval set of "ground truth" queries with known answers (canonical page X should always surface for query Y). Track pass rate.
- SLO: `self_eval_drift_7d` with target <15% drift.

## Dependencies + ordering

- #2 (speak split) should go first — it's scaffolding for #3 (brain_loop) and #5 (self_eval_drive) will need a new drive file.
- #1 (staleness) can run in parallel with anything — independent pipeline.
- #4 (override) depends on deciding the `brain_enforce` tag schema — probably 30 min design before coding.
- #3 (brain_loop) is the biggest structural change; do last after smaller refactors land.

## Success criteria (how do we know next sprint worked)

- No "search.py argparse" canonical surfacing within 24h of staleness_check running.
- `speak.py` doesn't exist as a single file anymore.
- `brain_loop` uptime log shows signal-triggered fires outnumbering tick fires.
Any attempt to edit protected Hermes profile config or Brain credentials without `BRAIN_OVERRIDE=1` returns `permissionDecision=deny`.
- `self_eval_drift_7d` SLO registered and reporting.
