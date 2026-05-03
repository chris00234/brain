# SLO remediation runbook

Brain SLO recovery is split into two layers:

1. `brain_core/slos.py` measures production budgets every scheduler tick.
2. `brain_core/slo_remediation.py` applies only deterministic, safe remediations.

No LLM is used for the direct playbook. Risky or credential-dependent failures stay manual.

## Direct playbook

| SLO | Direct action |
| --- | --- |
| `breaker_open_count` | manual-required; inspect `/brain/breakers`, reset only after successful provider probe |
| `outbox_pending_count` | trigger `outbox_drain` |
| `telegram_backlog_pending_count` | trigger `llm_backlog_drain` to replay direct Telegram backlog |
| `llm_backlog_pending` | trigger `llm_backlog_drain` legacy queue drain |
| `logs_dir_total_mb` | trigger `log_rotation` cleanup |
| `entry_contract_missing_pct` | trigger `entry_contract_audit` diagnostic |
| `qdrant_backup_age_hours` | trigger `qdrant_backup` |
| `neo4j_backup_age_hours` | trigger `neo4j_backup` |
| `backup_restore_drill_age_hours` | trigger `backup_restore_drill` |
| `brain_server_rss_mb` | pause heavy scheduler jobs for 30 minutes |
| `telegram_direct_health` | manual-required; token/chat/network failures cannot be safely self-fixed |

Every fired/manual action is appended to `logs/slo_remediation.jsonl`.
Disable the deterministic playbook with `BRAIN_SLO_AUTOREMEDIATE=off`.

## Alert delivery rule

Brain critical alerts use `brain_core/telegram_alert.py` direct Telegram Bot API. OpenClaw cron jobs that deliver to Telegram must use Chris's numeric chat id `8484060831`, not `Chris`/`@chris`. The daily `openclaw_telegram_target_audit` job and CI step enforce this.

## Backup restore assurance

- `backup_restore_drill` runs weekly and restores the latest local `brain-*.db.gz` and `autonomy-*.db.gz` into a temp dir, then runs `PRAGMA integrity_check`.
- `backup_restore_drill_age_hours` breaches if the latest successful drill is older than 192h.
- `backup_verify` remains the monthly Qdrant snapshot smoke test and now alerts through direct Telegram instead of OpenClaw.
