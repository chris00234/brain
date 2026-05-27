# Mission Control Center — Final Plan (2026-03-03)

## Confirmed Decisions
1. Project path: `~/server/mission-control-center`
2. Backend: separate `FastAPI` container
3. DB: `SQLite` first (personal use), migration-ready to Postgres
4. Object storage: existing `MinIO`
5. Redis: deferred for now
6. Assignees: Chris + all configured agents/subagents
7. Calendar scope: technical tasks only
8. Memory search: integrated across memory + linked tasks/content
9. Design direction: Option A + modern (Notion/Obsidian-like), clean/high-density/readable

## Final Architecture
- Frontend: Next.js 15 (App Router, TypeScript)
- API: FastAPI (containerized)
- Data: SQLite + Alembic migration baseline
- Assets: MinIO bucket for attachments
- Realtime: SSE + event log table
- Search: SQLite FTS5 (task/memory/content integrated)

## Module Scope (6)
1. Task Board
2. Content Pipeline
3. Calendar (tech-only)
4. Memory Screen + Unified Search
5. Team Structure
6. Digital Working Screen

## 7-Day Execution Plan
- Day 1: Monorepo skeleton, containers, env baseline
- Day 2: Core schema + migrations + task API
- Day 3: content/memory/attachment APIs
- Day 4: Task Board UI (kanban + assignee + status)
- Day 5: Memory screen + integrated search
- Day 6: Calendar + Team/Working screen v1
- Day 7: Integration QA + polish + docs

## Top Risks / Mitigation
- SQLite write contention → WAL + serialized writes
- scope creep → hard MVP boundary for week 1
- UI inconsistency → design tokens/components first
- migration risk → DB model abstraction + migration scripts from day 1

## Immediate Next Step
- Start Day 1 implementation now (skeleton + containers + baseline routes).
