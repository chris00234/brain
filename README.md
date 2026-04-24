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
