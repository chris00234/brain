# Brain

Chris's local memory and retrieval system. The FastAPI service runs on
`127.0.0.1:8791`, stores runtime state under `logs/`, and exposes recall,
learning, health, scheduling, SLO, and MCP-backed brain tools.

Start from these maps when changing the system:

- `brain/ARCHITECTURE.md` for the runtime architecture and data flow.
- `STORAGE_MAP.md` for authoritative storage locations.
- `CRON_MAP.md` and `brain_core/job_definitions.py` for scheduled jobs.
- `tests/unit/` for fast regression coverage.

Common verification:

```bash
.venv/bin/python -m pytest tests/unit
.venv/bin/ruff check <changed files>
```

## Agent access contract

Agents should use Brain through MCP tools first (`brain_recall`, `brain_store`,
`brain_decide`, `brain_reason`, `brain_tick`, `brain_procedures`,
`brain_outcome`). HTTP on `127.0.0.1:8791` is the fallback/admin surface for
endpoints not exposed through MCP, such as readiness, SLOs, job control, and
OpenClaw task execution evidence.

Autonomous/background LLM work is CLI-first through `brain_core/cli_llm.py`:
Codex `gpt-5.5` primary, `gpt-5.3-codex-spark` fallback, then configured
Claude accounts; OpenClaw is only the integration/emergency fallback lane.
`GET /brain/usage` reports this current `cli_llm` surface (`source=cli_llm`,
`primary_model=gpt-5.5`) rather than the legacy OpenClaw wrapper.

OpenClaw agent handoffs depend on the local gateway at `127.0.0.1:18789`; use
`/brain/ops/readiness`, `/brain/slos`, `/brain/autonomous-work`, and
`/brain/tasks/{task_id}/execution` to prove work actually dispatched before
claiming automation is running or done. The `autonomous_work_visibility_gap_count`
SLO must stay at 0 so background/no-prior-ack work is never hidden from the UI
or postmortems.
Task-evaluation decisions notify Chris only as action summaries from
`task_queue:evaluation_action_summary` (`TASK EVALUATION ACTION — Brain handled
these without asking`), not as requests for evaluation approval.
