# OpenClaw 에이전트 역할별 운영 정리 (2026-02-26)

- 업데이트 시각: 2026-02-26 22:43 (America/Los_Angeles)
- 기본 메인 에이전트: **Jenna** (`agents.list[].default=true`)
- 기준 파일: `~/.openclaw/openclaw.json`, `~/.openclaw/cron/jobs.json`, `workspace-*/config/mcporter.json`

## 공통 베이스라인

- 공통 활성 스킬(설정 기준): coding-agent, healthcheck, leonardo-ai, lobster, nano-banana-pro, tavily, tesla, todoist, weather
- 비활성 스킬: skill-creator, tmux
- 시스템 공통 MCP (`~/.mcporter/mcporter.json`): 비워둠 (agent-local config만 사용)

## Jenna

- 역할: 메인 오퍼레이터 / 라이프 매니저
- workspace: `/Users/chrischo/.openclaw/workspace-jenna`
- 기본 라우팅(default): ✅
- 도구 제한(tools.deny): gateway, nodes, canvas, browser

### MCP
- ✅ `context7` (ok)

### Skills
- 사용 가능 스킬 수(eligible): **27개**
- 목록: apple-notes, apple-reminders, clawhub, cloudflare, coding-agent, discord, gemini, gh-issues, gifgrep, github, healthcheck, imsg, leonardo-ai, lobster, mcp-integration, mcporter, model-usage, obsidian, openai-whisper, peekaboo, server-health, session-logs, tesla, things-mac, todoist, video-frames, weather

### Cron
- 등록 잡 수: **1개**
- ✅ **아침 브리핑 (7 AM)** — cron `0 7 * * *` (America/Los_Angeles)

## Ellie

- 역할: 인프라·운영 / 홈랩·자동화
- workspace: `/Users/chrischo/.openclaw/workspace-ellie`
- 기본 라우팅(default): ❌
- 도구 제한(tools.deny): (없음)

### MCP
- ✅ `obsidian` (ok)
- ✅ `context7` (ok) 해
- ✅ `github` (ok)
- ⚠️ `cloudflare` (offline)

### Skills
- 사용 가능 스킬 수(eligible): **27개**
- 목록: apple-notes, apple-reminders, clawhub, cloudflare, coding-agent, discord, gemini, gh-issues, gifgrep, github, healthcheck, imsg, leonardo-ai, lobster, mcp-integration, mcporter, model-usage, obsidian, openai-whisper, peekaboo, server-health, session-logs, tesla, things-mac, todoist, video-frames, weather

### Cron
- 등록 잡 수: **8개**
- ✅ **Automation Health Check** — every 1h
- ✅ **Email Alert Monitor (5m)** — every 5m
- ✅ **Obsidian MCP Singleton Guard (5m)** — every 5m
- ✅ **Obsidian Organizer** — cron `0 20 * * 0` (America/Los_Angeles)
- ✅ **OpenClaw Housekeeping (daily)** — cron `20 3 * * *` (America/Los_Angeles)
- ✅ **Security Guardrail Check** — every 12h
- ✅ **Server Mode macOS App Cleanup (30m + GUI-pass)** — every 30m
- ✅ **Weekly Cron Observability Report** — cron `0 18 * * 0` (America/Los_Angeles)

## Liz

- 역할: 개발·코드 / 아키텍처·리뷰
- workspace: `/Users/chrischo/.openclaw/workspace-liz`
- 기본 라우팅(default): ❌
- 도구 제한(tools.deny): gateway, nodes, canvas

### MCP
- ✅ `context7` (ok)
- ✅ `github` (ok)

### Skills
- 사용 가능 스킬 수(eligible): **27개**
- 목록: apple-notes, apple-reminders, clawhub, cloudflare, coding-agent, discord, gemini, gh-issues, gifgrep, github, healthcheck, imsg, leonardo-ai, lobster, mcp-integration, mcporter, model-usage, obsidian, openai-whisper, peekaboo, server-health, session-logs, tesla, things-mac, todoist, video-frames, weather

### Cron
- 등록 잡 수: **0개**
- (등록 없음)

## 메모

- 현재 구성상 Skills는 3개 에이전트 모두 동일한 공통 베이스를 사용한다.
- MCP는 agent-local `config/mcporter.json` 기준으로 역할 분리됨.
- Cloudflare MCP는 Ellie에만 붙어 있고 현재 offline 상태(인증/연결 복구 필요).
