# Personal AI Agent — Claude Telegram Bot

## Context

Build a personal AI assistant accessible via Telegram that can do anything on my Mac — powered by my existing Claude Max subscription. The bot invokes `claude -p` as a subprocess, so there's zero extra API cost. Send a message on Telegram, Claude Code runs locally, results come back to the chat.

## Architecture

```
Telegram (phone/desktop)
       │ long-polling
       ▼
┌─────────────────────────────┐
│  aiogram Bot (Python 3.11+) │  ← runs on Mac
│  ├─ Auth middleware          │
│  ├─ Rate limiter             │
│  └─ Task queue               │
└──────────┬──────────────────┘
           │ asyncio subprocess
           ▼
┌─────────────────────────────┐
│  claude -p --output-format  │  ← uses Max subscription
│  json --permission-mode     │
│  bypassPermissions          │
│  (stdin: prompt, stdout: JSON)
└──────────┬──────────────────┘
           │
           ▼
    Local filesystem,
    shell, tools, etc.
```

## Tech Stack

- **Python 3.11+** with **aiogram 3.x** (async Telegram framework)
- **Claude Code CLI** (`claude -p`) as subprocess
- **python-dotenv** for config
- **launchd** for auto-start on macOS
- No database, no Redis, no web server — minimal dependencies

## Project Structure

```
~/claude-telegram-bot/
├── pyproject.toml
├── .env                          # Telegram token, allowed user IDs
├── .env.example
├── .gitignore
├── scripts/
│   ├── install.sh                # Create venv, install deps, setup launchd
│   └── com.user.claude-telegram-bot.plist
├── src/
│   ├── __init__.py
│   ├── __main__.py               # Entry: python -m src
│   ├── config.py                 # Load env vars, validate
│   ├── bot.py                    # aiogram setup, dispatcher, polling
│   ├── handlers/
│   │   ├── commands.py           # /start, /help, /reset, /cancel, /status
│   │   └── messages.py           # Main flow: text → claude → response
│   ├── middleware/
│   │   ├── auth.py               # User allowlist (silent drop for strangers)
│   │   ├── throttle.py           # Rate limiting
│   │   └── logging_mw.py        # Request/response logging
│   ├── claude/
│   │   ├── subprocess_manager.py # Core: spawn claude -p, pipe stdin, parse JSON
│   │   ├── session_store.py      # Map chat_id → claude session_id
│   │   └── models.py             # ClaudeResult dataclass
│   ├── tasks/
│   │   └── task_queue.py         # asyncio task management, cancellation
│   └── utils/
│       ├── text_chunker.py       # Split responses for Telegram 4096 char limit
│       ├── markdown_converter.py # Claude markdown → Telegram HTML
│       └── sanitizer.py          # Input cleanup
└── tests/
    ├── conftest.py
    ├── test_subprocess_manager.py
    ├── test_text_chunker.py
    ├── test_sanitizer.py
    └── test_session_store.py
```

## Implementation Phases

### Phase 1: Project Skeleton
- Create project structure with `pyproject.toml` (deps: `aiogram>=3.23`, `python-dotenv`)
- Implement `config.py` — loads from `.env`: `TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_IDS`, `CLAUDE_BINARY`, `CLAUDE_MODEL`, `CLAUDE_WORKING_DIR`, `CLAUDE_TIMEOUT_SECONDS`
- Create `bot.py` — aiogram dispatcher with long-polling
- Add `__main__.py` entry point
- Add `/start` and `/help` commands
- **Verify**: Bot starts and responds to `/start`

### Phase 2: Claude Integration (core)
- Implement `subprocess_manager.py`:
  - Build command: `claude -p --output-format json --permission-mode bypassPermissions --model sonnet`
  - Spawn via `asyncio.create_subprocess_exec` (not `shell=True`)
  - Pipe prompt through stdin (avoids shell escaping, supports unlimited length)
  - Parse JSON response: extract `result`, `session_id`, `total_cost_usd`
  - Timeout via `asyncio.wait_for` (default 5 min), kill process on timeout
- Implement `models.py` — `ClaudeResult` dataclass
- Implement `messages.py` handler:
  1. Send "Thinking..." acknowledgment immediately
  2. Spawn claude subprocess in background
  3. Edit "Thinking..." message with the result
  4. If result > 4096 chars, send remaining chunks as new messages
- Implement `text_chunker.py` — split at paragraph > newline > space > hard cut
- **Verify**: Send message in Telegram, get Claude's response back

### Phase 3: Security & Robustness
- Implement `auth.py` middleware — check `message.from_user.id` against allowlist, silently drop unauthorized
- Implement `throttle.py` — min 2s between messages per user
- Implement `sanitizer.py` — strip null bytes, control chars
- Implement `logging_mw.py` — log all requests with timestamp, user_id, duration
- Add error handling: timeout → friendly message, process crash → error message
- **Verify**: Unauthorized user gets no response, rate limiting works

### Phase 4: Session Persistence
- Implement `session_store.py` — maps `chat_id → session_id`, **persisted to `data/sessions.json`**
  - Load from disk on startup, save on every change (atomic write: write tmp → rename)
  - Bot restart resumes exactly where you left off
- Wire `--resume <session_id>` into subprocess_manager when session exists
- Add `/reset` command — clears session, starts fresh
- **Verify**: Restart bot → send follow-up → context preserved

### Phase 5: Permanent Memory System
- `memory/memory_store.py` — long-term knowledge in `data/memory.json`:
  - Stores facts by category: personal, preferences, projects, technical
  - CRUD: add, search, list, delete. Atomic writes to prevent corruption.
- `memory/memory_injector.py` — prepends relevant memories to every Claude prompt via `--append-system-prompt`
- `memory/memory_extractor.py` — auto-extracts facts after each conversation using a lightweight haiku call (runs in background)
- Commands: `/remember <fact>`, `/memories`, `/forget <key>`
- Conversation summaries saved to `data/history/<session_id>.json` on `/reset`
- **Verify**: "I prefer Go" → `/reset` → new chat → "What language do I prefer?" → "Go"

### Phase 6: Task Management
- Implement `task_queue.py`:
  - One active task per chat (new message cancels previous)
  - Global semaphore (max 3 concurrent claude processes)
  - Cancellation support (kills subprocess)
- Add `/cancel` and `/status` commands
- **Verify**: `/cancel` works mid-task, concurrent limit enforced

### Phase 6: Polish & Deployment
- Implement `markdown_converter.py` — Claude markdown to Telegram HTML
- Add `/model` command — switch between sonnet/opus/haiku
- Create `scripts/install.sh` — venv, deps, launchd plist
- Create launchd plist — auto-start on login, auto-restart on crash
- **Verify**: Bot survives reboot, auto-restarts after crash

### Phase 7 (Optional): Streaming Progress
- Add `StreamingSubprocessManager` using `--output-format stream-json --verbose`
- Read stdout line-by-line, parse events, accumulate text
- Edit "Thinking..." message every 5 seconds with progress
- Show tool usage indicators ("Reading files...", "Running command...")

## Key Technical Decisions

| Decision | Choice | Why |
|----------|--------|-----|
| Claude invocation | `claude -p` via stdin pipe | Uses Max subscription (free), no shell injection, unlimited prompt length |
| Output format | `--output-format json` | Single JSON with result, session_id, cost — simple and reliable |
| Permission mode | `bypassPermissions` | Personal bot on own machine, full access desired |
| Telegram parse mode | `ParseMode.HTML` | More forgiving than MarkdownV2 |
| Concurrency | 1 task/chat, 3 global max | Prevents resource exhaustion |
| Session persistence | JSON file (`data/sessions.json`) | Survives restarts. Atomic writes prevent corruption. |
| Long-term memory | JSON file (`data/memory.json`) | Facts/preferences injected into every prompt via `--append-system-prompt` |
| Memory extraction | Auto via haiku model | Lightweight background call extracts new facts after each conversation |
| No database | Correct | JSON files sufficient for single-user, human-readable, easy to edit |

## Security

- **User allowlist**: Only my Telegram user ID can interact (silent drop for others)
- **No shell injection**: `create_subprocess_exec` (not `shell=True`), prompts via stdin pipe
- **Rate limiting**: 2s cooldown between messages
- **Resource caps**: 5 min timeout, $5/request budget, 3 max concurrent processes
- **Input sanitization**: Strip control chars, enforce max length
- **Logging**: All requests logged with user ID and timestamp

## Configuration (.env)

```bash
TELEGRAM_BOT_TOKEN=<from BotFather>
ALLOWED_USER_IDS=<my telegram user id>
CLAUDE_BINARY=/Users/chris/.local/bin/claude
CLAUDE_MODEL=sonnet
CLAUDE_PERMISSION_MODE=bypassPermissions
CLAUDE_WORKING_DIR=/Users/chris
CLAUDE_TIMEOUT_SECONDS=300
CLAUDE_MAX_BUDGET_USD=5.0
MAX_CONCURRENT_TASKS=3
```

## Prerequisites

1. Create a Telegram bot via @BotFather → get bot token
2. Get Telegram user ID (message @userinfobot)
3. Verify `claude` CLI is installed: `which claude`
4. Python 3.11+ installed

## Verification Checklist

- [ ] Phase 1: `/start` responds with welcome message
- [ ] Phase 2: "What is 2+2?" → get "4" back in Telegram
- [ ] Phase 3: Message from different account → no response
- [ ] Phase 4: Restart bot → send follow-up → conversation context preserved
- [ ] Phase 5: "I prefer Go" → `/reset` → new chat → "What language do I prefer?" → "Go"
- [ ] Phase 6: Send long task → `/cancel` → "Task cancelled"
- [ ] Phase 7: Kill bot process → auto-restarts within seconds

## Status
- **Created**: 2026-02-11
- **Status**: Planning complete, ready for implementation
