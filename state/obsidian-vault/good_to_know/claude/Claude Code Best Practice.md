# Claude Code Best Practice - The Ultimate Guide

> A comprehensive guide for developers who want to use Claude Code at its **full potential** — not just the CLI, but the entire ecosystem: agents, skills, hooks, MCP servers, plugins, CLAUDE.md, configuration, workflows, and frameworks like SuperClaude.

---

## Table of Contents

1. [[#1. What Is Claude Code Really]]
2. [[#2. The Configuration Hierarchy]]
3. [[#3. CLAUDE.md — The Constitution]]
4. [[#4. Settings and Permissions]]
5. [[#5. Custom Subagents]]
6. [[#6. Skills (Custom Slash Commands)]]
7. [[#7. Hooks — Deterministic Automation]]
8. [[#8. MCP Servers — External Tool Integration]]
9. [[#9. Plugins — Bundled Extensions]]
10. [[#10. Keyboard Shortcuts and CLI Flags]]
11. [[#11. Prompt Engineering for Claude Code]]
12. [[#12. Context Window Management]]
13. [[#13. Workflow Patterns]]
14. [[#14. Parallel Sessions and Scaling]]
15. [[#15. SuperClaude Framework]]
16. [[#16. Environment Variables Reference]]
17. [[#17. Common Anti-Patterns]]
18. [[#18. Resources and Community]]

---

## 1. What Is Claude Code Really

Claude Code is NOT just a chatbot in your terminal. It is a **full agentic coding environment** that can:

- Read, write, and edit files across your entire project
- Execute shell commands (build, test, deploy)
- Search codebases with Grep, Glob, and advanced pattern matching
- Browse the web and fetch documentation
- Delegate to specialized subagents that run in isolated contexts
- Connect to external tools via MCP (Model Context Protocol)
- Run headlessly in CI/CD pipelines
- Manage persistent memory across sessions
- Coordinate multiple parallel sessions (Agent Teams)

**Key mental model**: You describe WHAT you want → Claude figures out HOW to build it.

### Models Available

| Model | ID | Best For |
|-------|-----|---------|
| **Opus 4.6** | `claude-opus-4-6` | Most capable, complex architecture, deep analysis |
| **Sonnet 4.5** | `claude-sonnet-4-5-20250929` | Daily coding, balanced capability/speed |
| **Haiku 4.5** | `claude-haiku-4-5-20251001` | Fast tasks, quick searches, subagent exploration |

Switch models: `/model` in session, `--model` flag on CLI, or `ANTHROPIC_MODEL` env var.

---

## 2. The Configuration Hierarchy

Claude Code uses a **4-level scope system** (highest to lowest precedence):

| Scope | Location | Who | Shared? |
|-------|----------|-----|---------|
| **Managed** | System-level `managed-settings.json` | All users on machine | IT-deployed |
| **Local** | `.claude/settings.local.json` | Current repo only | No (gitignored) |
| **Project** | `.claude/settings.json` | Repo collaborators | Yes (committed) |
| **User** | `~/.claude/settings.json` | All your projects | No |

### System Paths for Managed Settings

- **macOS**: `/Library/Application Support/ClaudeCode/`
- **Linux/WSL**: `/etc/claude-code/`

### Key Configuration Files

| File | Purpose |
|------|---------|
| `~/.claude/settings.json` | User-level settings (permissions, hooks, env) |
| `.claude/settings.json` | Project-level settings (shared with team) |
| `.claude/settings.local.json` | Local project overrides (gitignored) |
| `~/.claude.json` | Preferences, OAuth, MCP servers, caches |
| `~/.claude/CLAUDE.md` | Global instructions for ALL sessions |
| `./CLAUDE.md` | Project-level instructions |
| `./CLAUDE.local.md` | Local project instructions (gitignored) |

---

## 3. CLAUDE.md — The Constitution

**CLAUDE.md is the single most important file for using Claude Code effectively.** It is Claude's "constitution" — the persistent context loaded at the start of every session.

### How to Start

Run `/init` to auto-generate a starter CLAUDE.md based on your project structure. Then refine it over time.

### What to Include

```markdown
# Project: MyApp

## Build & Test Commands
- Build: `npm run build`
- Test single: `npm test -- --testPathPattern=<file>`
- Lint: `npm run lint`
- Type check: `npx tsc --noEmit`

## Code Style
- Use ES modules (import/export), not CommonJS (require)
- Destructure imports: `import { foo } from 'bar'`
- Use TypeScript strict mode

## Architecture
- Monorepo: apps/web, apps/api, packages/shared
- State management: Zustand (NOT Redux)
- API: tRPC with Zod validation

## Workflow Rules
- Always run typecheck after code changes
- Prefer single test runs over full suite
- Use feature branches, never push to main directly
- Commit message format: `type(scope): description`

## Common Gotchas
- The `auth` middleware requires `SESSION_SECRET` env var
- Database migrations must be run before tests: `npm run db:migrate`
```

### What NOT to Include

- Things Claude can figure out from reading code
- Standard language conventions (Claude already knows them)
- Detailed API documentation (link to docs instead)
- Information that changes frequently
- Long explanations or tutorials
- Code style that should be handled by linters/formatters

### CLAUDE.md Locations (All Loaded)

| Location | Use Case |
|----------|----------|
| `~/.claude/CLAUDE.md` | Global instructions for all projects |
| `./CLAUDE.md` | Project-wide (check into git) |
| `./CLAUDE.local.md` | Personal overrides (gitignore it) |
| `./subdir/CLAUDE.md` | Subdirectory-specific (loaded on demand) |
| Parent directories | Monorepo root + child CLAUDE.md |

### Import Syntax

```markdown
See @README.md for project overview.
Git workflow: @docs/git-instructions.md
Personal overrides: @~/.claude/my-project-instructions.md
```

### Pro Tips

- **Emphasis matters**: Use "IMPORTANT" or "YOU MUST" for critical rules
- **Keep it short**: If Claude ignores a rule, your CLAUDE.md is probably too long
- **Treat it like code**: Review when things go wrong, prune regularly
- **Test changes**: Observe whether Claude's behavior actually shifts after edits

---

## 4. Settings and Permissions

### Permission Configuration

```json
{
  "permissions": {
    "allow": [
      "Bash(npm run lint)",
      "Bash(npm run test *)",
      "Bash(git commit *)",
      "Read(~/.zshrc)"
    ],
    "ask": [
      "Bash(git push *)"
    ],
    "deny": [
      "Bash(curl *)",
      "Read(./.env)",
      "Read(./.env.*)",
      "Read(./secrets/**)",
      "WebFetch"
    ],
    "additionalDirectories": ["../docs/"],
    "defaultMode": "acceptEdits"
  }
}
```

### Permission Rule Syntax

| Rule | Matches |
|------|---------|
| `Bash` | All bash commands |
| `Bash(npm run *)` | Commands starting with `npm run` |
| `Read(./.env)` | Reading specific file |
| `Edit(./src/**)` | Edit any file in src/ recursively |
| `WebFetch(domain:example.com)` | Fetch from specific domain |
| `MCP(toolName)` | Specific MCP tool |
| `Task(agent-name)` | Specific subagent |
| `Skill(skill-name)` | Specific skill |

**Evaluation Order**: Deny > Ask > Allow (first match wins)

### Permission Modes

| Mode | Behavior |
|------|----------|
| `default` | Standard permission checking with prompts |
| `acceptEdits` | Auto-accept file edits |
| `dontAsk` | Auto-deny permissions (allowed tools still work) |
| `bypassPermissions` | Skip all permission checks |
| `plan` | Plan mode (read-only exploration) |

### Useful Settings

```json
{
  "model": "claude-sonnet-4-5-20250929",
  "alwaysThinkingEnabled": true,
  "language": "english",
  "cleanupPeriodDays": 30,
  "env": {
    "NODE_ENV": "development"
  },
  "attribution": {
    "commit": "Co-Authored-By: Claude <noreply@anthropic.com>",
    "pr": "Generated with Claude Code"
  }
}
```

---

## 5. Custom Subagents

Subagents are **specialized AI assistants** that run in their own context window with custom system prompts, specific tool access, and independent permissions.

### Why Subagents?

- **Preserve context**: Exploration doesn't pollute your main conversation
- **Enforce constraints**: Limit which tools a subagent can use
- **Specialize**: Focused system prompts for specific domains
- **Control costs**: Route tasks to cheaper models like Haiku

### Built-in Subagents

| Agent | Model | Tools | Purpose |
|-------|-------|-------|---------|
| **Explore** | Haiku | Read-only | Fast codebase search and exploration |
| **Plan** | Inherits | Read-only | Research for plan mode |
| **General-purpose** | Inherits | All | Complex multi-step tasks |
| **Bash** | Inherits | Bash | Terminal commands in separate context |

### Creating Custom Subagents

**Location**: `~/.claude/agents/` (user-level) or `.claude/agents/` (project-level)

**Interactive**: Run `/agents` → Create new agent

**Manual**: Create a `.md` file:

```markdown
---
name: security-reviewer
description: Reviews code for security vulnerabilities. Use proactively after code changes.
tools: Read, Grep, Glob, Bash
model: opus
---

You are a senior security engineer. Review code for:
- Injection vulnerabilities (SQL, XSS, command injection)
- Authentication and authorization flaws
- Secrets or credentials in code
- Insecure data handling

Provide specific line references and suggested fixes.
```

### Frontmatter Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique identifier (lowercase, hyphens) |
| `description` | Yes | When Claude should delegate (write clearly!) |
| `tools` | No | Allowed tools (inherits all if omitted) |
| `disallowedTools` | No | Tools to deny |
| `model` | No | `sonnet`, `opus`, `haiku`, or `inherit` |
| `permissionMode` | No | `default`, `acceptEdits`, `dontAsk`, `bypassPermissions`, `plan` |
| `skills` | No | Skills to preload into context at startup |
| `hooks` | No | Lifecycle hooks scoped to this subagent |
| `memory` | No | Persistent memory: `user`, `project`, or `local` |

### Persistent Memory for Subagents

```markdown
---
name: code-reviewer
description: Reviews code for quality and best practices
memory: user
---
```

Memory scopes:

| Scope | Location | Use When |
|-------|----------|----------|
| `user` | `~/.claude/agent-memory/<name>/` | Learnings across all projects |
| `project` | `.claude/agent-memory/<name>/` | Project-specific, shareable via git |
| `local` | `.claude/agent-memory-local/<name>/` | Project-specific, not committed |

### CLI-defined Subagents (Temporary)

```bash
claude --agents '{
  "quick-reviewer": {
    "description": "Quick code review",
    "prompt": "You are a code reviewer. Focus on bugs and security.",
    "tools": ["Read", "Grep", "Glob"],
    "model": "haiku"
  }
}'
```

### Using Subagents

```
# Automatic delegation (Claude decides based on description)
Review this code for security issues

# Explicit delegation
Use the security-reviewer agent to review src/auth/

# Background execution
Run this in the background using a subagent

# Parallel research
Research the auth, database, and API modules in parallel using separate subagents
```

### Foreground vs Background

- **Foreground**: Blocks main conversation, permission prompts pass through to you
- **Background**: Runs concurrently (Ctrl+B to background a running task), auto-denies unpermitted actions

---

## 6. Skills (Custom Slash Commands)

Skills extend Claude's capabilities with domain knowledge and reusable workflows. They are `.claude/skills/<name>/SKILL.md` files with YAML frontmatter.

> Note: `.claude/commands/` still works and is equivalent. Skills are the modern replacement with extra features.

### Creating a Skill

```bash
mkdir -p .claude/skills/fix-issue
```

```markdown
# .claude/skills/fix-issue/SKILL.md
---
name: fix-issue
description: Fix a GitHub issue by number
disable-model-invocation: true
---

Fix GitHub issue $ARGUMENTS:

1. Use `gh issue view $ARGUMENTS` to get details
2. Understand the problem
3. Search codebase for relevant files
4. Implement the fix
5. Write and run tests
6. Create a descriptive commit
7. Push and create a PR
```

Invoke: `/fix-issue 1234`

### Skill Locations

| Location | Scope |
|----------|-------|
| `~/.claude/skills/<name>/SKILL.md` | All your projects (personal) |
| `.claude/skills/<name>/SKILL.md` | This project only |
| Plugin `skills/<name>/SKILL.md` | Where plugin is enabled |

### Frontmatter Fields

| Field | Description |
|-------|-------------|
| `name` | Display name / slash command name |
| `description` | When Claude should use it (be specific!) |
| `argument-hint` | Autocomplete hint: `[issue-number]` |
| `disable-model-invocation` | `true` = only YOU can invoke (for deploy, commit, etc.) |
| `user-invocable` | `false` = only Claude can invoke (background knowledge) |
| `allowed-tools` | Tools available when skill is active |
| `model` | Model to use |
| `context` | `fork` = run in isolated subagent context |
| `agent` | Which subagent to use with `context: fork` |
| `hooks` | Lifecycle hooks scoped to this skill |

### Variable Substitutions

| Variable | Description |
|----------|-------------|
| `$ARGUMENTS` | All arguments passed to the skill |
| `$ARGUMENTS[0]` / `$0` | First argument |
| `$ARGUMENTS[1]` / `$1` | Second argument |
| `${CLAUDE_SESSION_ID}` | Current session ID |

### Dynamic Context Injection

Use `` !`command` `` to run shell commands before sending to Claude:

```markdown
---
name: pr-summary
description: Summarize current PR
context: fork
agent: Explore
---

## PR Context
- Diff: !`gh pr diff`
- Comments: !`gh pr view --comments`
- Changed files: !`gh pr diff --name-only`

Summarize this pull request.
```

### Running Skills in Subagents

Add `context: fork` to run in isolation (won't see your conversation history):

```markdown
---
name: deep-research
description: Research a topic thoroughly
context: fork
agent: Explore
---

Research $ARGUMENTS thoroughly:
1. Find relevant files using Glob and Grep
2. Read and analyze the code
3. Summarize findings with file references
```

### Supporting Files

```
my-skill/
├── SKILL.md           # Main instructions (required)
├── template.md        # Template for Claude to fill in
├── examples/
│   └── sample.md      # Example output
└── scripts/
    └── validate.sh    # Script Claude can execute
```

Reference from SKILL.md: `See [reference.md](reference.md) for API details`

**Keep SKILL.md under 500 lines.** Move detailed reference to separate files.

---

## 7. Hooks — Deterministic Automation

Hooks are **shell commands or LLM prompts** that execute automatically at specific points in Claude's lifecycle. Unlike CLAUDE.md instructions (advisory), hooks are **deterministic** — they guarantee the action happens.

### Hook Events (Lifecycle)

| Event | When | Can Block? |
|-------|------|-----------|
| `SessionStart` | Session begins/resumes | No |
| `UserPromptSubmit` | User submits prompt, before processing | Yes |
| `PreToolUse` | Before a tool call executes | Yes |
| `PermissionRequest` | Permission dialog appears | Yes |
| `PostToolUse` | After tool call succeeds | No |
| `PostToolUseFailure` | After tool call fails | No |
| `Notification` | Notification sent | No |
| `SubagentStart` | Subagent spawned | No |
| `SubagentStop` | Subagent finished | Yes |
| `Stop` | Claude finishes responding | Yes |
| `PreCompact` | Before context compaction | No |
| `SessionEnd` | Session terminates | No |

### Configuration

Hooks go in `settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": ".claude/hooks/validate-bash.sh"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [
          {
            "type": "command",
            "command": ".claude/hooks/auto-lint.sh"
          }
        ]
      }
    ]
  }
}
```

### Hook Types

| Type | Description |
|------|-------------|
| `command` | Run a shell command (receives JSON on stdin) |
| `prompt` | Send prompt to LLM for yes/no evaluation |
| `agent` | Spawn subagent with tools (Read, Grep, Glob) to verify |

### Exit Codes

| Code | Meaning |
|------|---------|
| **0** | Success — allow the action, parse JSON from stdout |
| **2** | Blocking error — block the action, stderr shown to Claude |
| **Other** | Non-blocking error — continue, show stderr in verbose mode |

### Matcher Patterns

Matchers are regex strings:

| Event | Matches On | Examples |
|-------|-----------|---------|
| Tool events | Tool name | `Bash`, `Edit\|Write`, `mcp__.*` |
| `SessionStart` | Session source | `startup`, `resume`, `clear` |
| `SessionEnd` | Exit reason | `clear`, `logout`, `prompt_input_exit` |
| `Notification` | Type | `permission_prompt`, `idle_prompt` |
| `SubagentStart/Stop` | Agent type | `Explore`, `Plan`, custom names |

### Example: Auto-lint After File Changes

```bash
#!/bin/bash
# .claude/hooks/auto-lint.sh
INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

if [[ "$FILE_PATH" == *.ts || "$FILE_PATH" == *.tsx ]]; then
  npx eslint --fix "$FILE_PATH" 2>&1
fi
exit 0
```

### Example: Block Destructive Commands

```bash
#!/bin/bash
# .claude/hooks/block-rm.sh
COMMAND=$(jq -r '.tool_input.command')
if echo "$COMMAND" | grep -q 'rm -rf'; then
  jq -n '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: "Destructive command blocked"
    }
  }'
else
  exit 0
fi
```

### Prompt-Based Hooks

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "prompt",
            "prompt": "Evaluate if all tasks are complete: $ARGUMENTS. Check tests pass.",
            "timeout": 30
          }
        ]
      }
    ]
  }
}
```

### Agent-Based Hooks

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "agent",
            "prompt": "Verify all unit tests pass. Run the test suite. $ARGUMENTS",
            "timeout": 120
          }
        ]
      }
    ]
  }
}
```

### Async Hooks

Add `"async": true` to run in background without blocking:

```json
{
  "type": "command",
  "command": ".claude/hooks/run-tests.sh",
  "async": true,
  "timeout": 300
}
```

### Managing Hooks

- **Interactive**: `/hooks` command
- **Manual**: Edit `.claude/settings.json`
- **Ask Claude**: "Write a hook that runs eslint after every file edit"
- **Debug**: `claude --debug` to see hook execution details
- **Verbose**: `Ctrl+O` to see hook progress in session

---

## 8. MCP Servers — External Tool Integration

MCP (Model Context Protocol) servers let Claude interact with external services: databases, APIs, design tools, browsers, and more.

### Configuration

MCP servers go in `~/.claude.json` (user-level) or `.mcp.json` (project-level):

```json
{
  "mcpServers": {
    "github": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {
        "GITHUB_TOKEN": "your-token"
      }
    }
  }
}
```

### Managing MCP Servers

```bash
# Add via CLI
claude mcp add my-server -e API_KEY=123 -- npx @some/mcp-server

# List configured servers
claude mcp list

# Remove a server
claude mcp remove my-server
```

### Popular MCP Servers

| Server | Purpose | Package |
|--------|---------|---------|
| **Context7** | Library documentation lookup | `@upstash/context7-mcp` |
| **Playwright** | Browser automation & E2E testing | `@executeautomation/playwright-mcp-server` |
| **Sequential Thinking** | Multi-step reasoning | `@modelcontextprotocol/server-sequential-thinking` |
| **Magic (21st.dev)** | UI component generation | `@21st-dev/magic@latest` |
| **Serena** | Semantic code understanding | `serena` (via uvx) |
| **Morphllm** | Bulk code transformations | `@morph-llm/morph-fast-apply` |
| **GitHub** | GitHub API integration | `@modelcontextprotocol/server-github` |
| **Filesystem** | File system operations | `@modelcontextprotocol/server-filesystem` |
| **Obsidian** | Obsidian vault access | `@mauricio.wolff/mcp-obsidian@latest` |

### MCP Tool Naming Convention

Tools follow: `mcp__<server>__<tool>`

Examples:
- `mcp__memory__create_entities`
- `mcp__filesystem__read_file`
- `mcp__github__search_repositories`

### MCP Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `MCP_TIMEOUT` | Server startup timeout (ms) | — |
| `MCP_TOOL_TIMEOUT` | Tool execution timeout (ms) | — |
| `MAX_MCP_OUTPUT_TOKENS` | Max response tokens | 25000 |
| `ENABLE_TOOL_SEARCH` | Tool search mode | `auto` |

---

## 9. Plugins — Bundled Extensions

Plugins package skills, hooks, subagents, and MCP servers into a single installable unit.

### Installing Plugins

```
# Browse marketplace
/plugin

# Install from marketplace
/plugin marketplace add owner/repo
/plugin install plugin-name@marketplace-name
```

### Plugin Structure

```
.claude-plugin/
├── plugin.json        # Plugin manifest
├── commands/          # Slash commands
├── agents/            # Subagents
├── skills/            # Skills
├── hooks/hooks.json   # Hooks (auto-loaded in v2.1+)
├── .mcp.json          # MCP server configs
└── README.md
```

### Notable Community Plugins

- **everything-claude-code** (40.7k stars): Complete config collection — agents, skills, hooks, commands, rules
- **claude-code-showcase**: Comprehensive example with hooks, skills, agents, commands, GitHub Actions

### Plugin Settings

```json
{
  "enabledPlugins": {
    "formatter@acme-tools": true,
    "deployer@acme-tools": true
  },
  "extraKnownMarketplaces": {
    "acme-tools": {
      "source": {
        "source": "github",
        "repo": "acme-corp/claude-plugins"
      }
    }
  }
}
```

---

## 10. Keyboard Shortcuts and CLI Flags

### In-Session Shortcuts

| Shortcut | Action |
|----------|--------|
| `Esc` | Stop Claude mid-action (context preserved) |
| `Esc + Esc` | Open rewind/checkpoint menu |
| `Ctrl+G` | Open plan in editor |
| `Ctrl+O` | Toggle verbose mode (see thinking) |
| `Ctrl+B` | Background a running task |
| `Option+T` / `Alt+T` | Toggle extended thinking |
| `Shift+Enter` | Multi-line input |
| `!` | Enter shell mode |

### Slash Commands

| Command | Purpose |
|---------|---------|
| `/help` | Show all commands |
| `/init` | Generate starter CLAUDE.md |
| `/config` | Interactive settings |
| `/permissions` | Manage permissions |
| `/hooks` | Configure hooks |
| `/agents` | Manage subagents |
| `/mcp` | Manage MCP servers |
| `/model` | Switch model |
| `/compact` | Compact context (with optional instructions) |
| `/clear` | Reset context window |
| `/rewind` | Restore previous checkpoint |
| `/rename` | Name current session |
| `/context` | Show context usage |
| `/plugin` | Browse plugin marketplace |
| `/vim` | Enable vim editing mode |
| `/terminal-setup` | Install keyboard shortcuts |
| `/statusline` | Configure status line |
| `/sandbox` | Enable OS-level sandboxing |

### CLI Flags

| Flag | Purpose | Example |
|------|---------|---------|
| `--continue` / `-c` | Resume most recent conversation | `claude -c` |
| `--resume` / `-r` | Select from recent sessions | `claude --resume` |
| `-p "prompt"` | Print mode (headless, non-interactive) | `claude -p "explain this"` |
| `--model` | Specify model | `--model claude-opus-4-6` |
| `--add-dir` | Include additional directories | `--add-dir ../lib ../docs` |
| `--allowedTools` | Permit specific tools | `--allowedTools "Write" "Bash(git *)"` |
| `--disallowedTools` | Block specific tools | `--disallowedTools "Bash(rm *)"` |
| `--max-turns` | Limit conversation rounds | `--max-turns 5` |
| `--output-format` | Response format | `--output-format json` |
| `--input-format` | Input format | `--input-format stream-json` |
| `--verbose` | Detailed logging | `--verbose` |
| `--agents` | Pass temporary agent JSON | `--agents '{...}'` |
| `--dangerously-skip-permissions` | Skip all permission checks | Use in sandbox only! |

---

## 11. Prompt Engineering for Claude Code

### The Most Important Practice

**Give Claude a way to verify its work.** This is the single highest-leverage thing you can do.

| Bad | Good |
|-----|------|
| "implement email validation" | "write a validateEmail function. test cases: user@example.com = true, invalid = false, user@.com = false. run the tests after implementing" |
| "make the dashboard look better" | "[paste screenshot] implement this design. take a screenshot of the result and compare to the original" |
| "the build is failing" | "the build fails with this error: [paste error]. fix it and verify the build succeeds" |

### Providing Context

- **`@` file references**: `@./src/auth/login.ts` (Claude reads the file)
- **Paste images**: Drag-and-drop screenshots directly
- **Give URLs**: Link to documentation
- **Pipe data**: `cat error.log | claude`
- **Let Claude fetch**: Tell Claude to pull its own context via tools

### Let Claude Interview You

For larger features, start with:

```
I want to build [brief description]. Interview me in detail using the AskUserQuestion tool.

Ask about technical implementation, UI/UX, edge cases, and tradeoffs.
Keep interviewing until we've covered everything, then write a complete spec to SPEC.md.
```

Then start a **fresh session** to implement from the spec.

### Effective Patterns

| Pattern | When |
|---------|------|
| "Explore first, then plan, then code" | Uncertain about approach |
| "Use Plan Mode" | Multi-file changes, unfamiliar code |
| "Use subagents to investigate X" | Keep main context clean |
| "Write a failing test first, then implement" | Clear expected behavior |
| "Look at how X is implemented and follow the pattern" | Consistent codebase |

---

## 12. Context Window Management

> **Context is your most important resource.** Performance degrades as it fills.

### Monitor Context

- Use `/context` to check usage
- Configure a custom status line to show token usage
- Watch for auto-compaction warnings

### Strategies

| Strategy | How |
|----------|-----|
| `/clear` between tasks | Reset context for unrelated work |
| `/compact <instructions>` | Summarize with focus: `/compact Focus on API changes` |
| Use subagents for research | They explore in separate context, return summaries |
| Scope investigations | "Check only src/auth/" not "investigate the codebase" |
| Fresh session + better prompt | After 2+ failed corrections, start clean |

### CLAUDE.md Compaction Instructions

Add to your CLAUDE.md:

```markdown
When compacting, always preserve:
- The full list of modified files
- All test commands that were run
- Any architectural decisions made
```

### Session Management

```bash
claude --continue    # Resume most recent
claude --resume      # Select from recent sessions
```

Use `/rename` to name sessions: "oauth-migration", "debugging-memory-leak"

---

## 13. Workflow Patterns

### The Standard Workflow

```
1. Explore    → Plan Mode: read files, understand code
2. Plan       → Ask Claude to create implementation plan
3. Implement  → Switch to Normal Mode, execute with verification
4. Commit     → Ask Claude to commit with descriptive message and PR
```

### Writer/Reviewer Pattern (Two Sessions)

| Session A (Writer) | Session B (Reviewer) |
|--------------------|--------------------|
| "Implement rate limiter for API" | |
| | "Review rate limiter in @src/middleware/rateLimiter.ts. Look for edge cases, race conditions" |
| "Here's review feedback: [paste]. Address these issues" | |

### Test-First Pattern

```
Write tests for the OAuth callback handler based on these requirements: [requirements].
Do NOT implement the handler yet, just the tests.
```

Then in new session:
```
Implement the OAuth callback handler. Make all tests in @tests/auth.test.ts pass.
```

### Fan-Out Pattern (Batch Processing)

```bash
# Generate task list
claude -p "List all Python files needing migration" > files.txt

# Process each file in parallel
for file in $(cat files.txt); do
  claude -p "Migrate $file from React to Vue. Return OK or FAIL." \
    --allowedTools "Edit,Bash(git commit *)" &
done
wait
```

### CI/CD Integration

```bash
# Headless mode in pipeline
claude -p "Review this PR for security issues" --output-format json
claude -p "Generate changelog from recent commits" --output-format json
```

---

## 14. Parallel Sessions and Scaling

### Multiple Sessions

- **Claude Desktop**: Multiple local sessions visually
- **Claude Code on Web**: Cloud infrastructure, isolated VMs
- **Agent Teams**: Automated coordination with shared tasks and messaging

### Agent Teams

Multiple sessions working together with a team lead coordinating:

```bash
# Enable experimental agent teams
export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1
```

### Headless Mode

```bash
# One-off queries
claude -p "Explain what this project does"

# Structured output
claude -p "List all API endpoints" --output-format json

# Streaming for real-time
claude -p "Analyze this log file" --output-format stream-json
```

---

## 15. SuperClaude Framework

SuperClaude is a **framework extension** for Claude Code that adds structured behavioral modes, MCP server orchestration, and task management patterns.

### What SuperClaude Provides

Your current SuperClaude setup includes these components loaded via `~/.claude/CLAUDE.md`:

#### Core Framework Files

| File | Purpose |
|------|---------|
| `FLAGS.md` | Behavioral flags (`--think`, `--ultrathink`, `--brainstorm`, etc.) |
| `PRINCIPLES.md` | Software engineering principles (SOLID, DRY, KISS, YAGNI) |
| `RULES.md` | Actionable behavioral rules with priority system |

#### Behavioral Modes

| Mode | Trigger | Purpose |
|------|---------|---------|
| **Brainstorming** | Vague requests, "maybe", "thinking about" | Collaborative discovery via Socratic dialogue |
| **Introspection** | Self-analysis, error recovery | Meta-cognitive analysis with transparency markers |
| **Orchestration** | Multi-tool ops, performance constraints | Intelligent tool selection and parallel execution |
| **Task Management** | >3 steps, complex scope | Hierarchical task organization with memory |
| **Token Efficiency** | Context >75%, `--uc` flag | Symbol-enhanced communication, 30-50% reduction |
| **Business Panel** | Business analysis, strategy | Multi-expert panel (Porter, Christensen, Drucker, etc.) |

#### MCP Server Documentation

| File | Server | Purpose |
|------|--------|---------|
| `MCP_Context7.md` | Context7 | Library docs lookup |
| `MCP_Magic.md` | Magic (21st.dev) | UI component generation |
| `MCP_Morphllm.md` | Morphllm | Bulk code transformations |
| `MCP_Playwright.md` | Playwright | Browser automation & testing |
| `MCP_Sequential.md` | Sequential | Multi-step reasoning |
| `MCP_Serena.md` | Serena | Semantic code understanding |

### SuperClaude Flags

| Flag | Purpose |
|------|---------|
| `--brainstorm` | Activate collaborative discovery mindset |
| `--introspect` | Expose thinking process with markers |
| `--task-manage` | Hierarchical task organization |
| `--orchestrate` | Optimize tool selection |
| `--token-efficient` / `--uc` | Symbol-enhanced communication |
| `--think` | Standard structured analysis (~4K tokens) |
| `--think-hard` | Deep analysis (~10K tokens) |
| `--ultrathink` | Maximum depth (~32K tokens) |
| `--c7` / `--context7` | Enable Context7 for docs |
| `--seq` / `--sequential` | Enable Sequential for reasoning |
| `--magic` | Enable Magic for UI |
| `--morph` | Enable Morphllm for bulk edits |
| `--serena` | Enable Serena for semantic understanding |
| `--play` / `--playwright` | Enable Playwright for browser |
| `--all-mcp` | Enable all MCP servers |
| `--no-mcp` | Disable all MCP servers |

### SuperClaude Skills (Custom Commands)

Located in `~/.claude/commands/sc/`:

| Skill | Purpose |
|-------|---------|
| `/sc:load` | Initialize session with project context |
| `/sc:save` | Save session context and memory |
| `/sc:analyze` | Comprehensive code analysis |
| `/sc:implement` | Feature implementation with persona activation |
| `/sc:improve` | Systematic code improvements |
| `/sc:explain` | Clear explanations of code and concepts |
| `/sc:troubleshoot` | Diagnose and resolve issues |
| `/sc:test` | Execute tests with coverage analysis |
| `/sc:build` | Build/compile with error handling |
| `/sc:design` | Design system architecture and APIs |
| `/sc:cleanup` | Remove dead code, optimize structure |
| `/sc:git` | Git operations with intelligent commits |
| `/sc:task` | Complex task execution and delegation |
| `/sc:brainstorm` | Interactive requirements discovery |
| `/sc:workflow` | Generate implementation workflows |
| `/sc:select-tool` | Intelligent MCP tool selection |
| `/sc:help` | List all /sc commands |
| `/sc:business-panel` | Multi-expert business analysis |
| `/sc:reflect` | Task reflection and validation |
| `/sc:index` | Generate project documentation |
| `/sc:spawn` | Meta-system task orchestration |
| `/sc:estimate` | Development estimates |
| `/sc:spec-panel` | Multi-expert specification review |
| `/sc:document` | Generate focused documentation |

### SuperClaude Agents

Located in `~/.claude/agents/`:

| Agent | Purpose |
|-------|---------|
| `backend-architect` | Reliable backend systems design |
| `frontend-architect` | Accessible, performant UIs |
| `system-architect` | Scalable system architecture |
| `security-engineer` | Security vulnerabilities and compliance |
| `performance-engineer` | Measurement-driven optimization |
| `quality-engineer` | Testing strategies and edge cases |
| `devops-architect` | Infrastructure and deployment automation |
| `python-expert` | Production-ready Python code |
| `refactoring-expert` | Code quality via systematic refactoring |
| `technical-writer` | Clear technical documentation |
| `requirements-analyst` | Transform ideas into specifications |
| `root-cause-analyst` | Evidence-based problem investigation |
| `socratic-mentor` | Programming education via questioning |
| `learning-guide` | Concept teaching and code explanation |
| `business-panel-experts` | Multi-expert business strategy |

### SuperClaude Symbol System

Token-efficient communication using symbols:

| Symbol | Meaning |
|--------|---------|
| `→` | leads to, implies |
| `⇒` | transforms to |
| `∴` | therefore |
| `∵` | because |
| `✅` | completed, passed |
| `❌` | failed, error |
| `⚠️` | warning |
| `🔄` | in progress |
| `⚡` | performance |
| `🔍` | analysis |
| `🛡️` | security |
| `🏗️` | architecture |

---

## 16. Environment Variables Reference

### Authentication & API

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` | API key |
| `ANTHROPIC_MODEL` | Model override |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | Haiku model override |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | Sonnet model override |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` | Opus model override |

### Tool & Bash

| Variable | Purpose |
|----------|---------|
| `BASH_DEFAULT_TIMEOUT_MS` | Default bash timeout |
| `BASH_MAX_TIMEOUT_MS` | Max bash timeout |
| `BASH_MAX_OUTPUT_LENGTH` | Max output chars |
| `CLAUDE_CODE_SHELL` | Override shell |

### Model & Performance

| Variable | Purpose | Default |
|----------|---------|---------|
| `CLAUDE_CODE_MAX_OUTPUT_TOKENS` | Max output (1-64000) | 32000 |
| `CLAUDE_CODE_EFFORT_LEVEL` | `low`, `medium`, `high` | `high` |
| `CLAUDE_CODE_SUBAGENT_MODEL` | Subagent model override | — |
| `MAX_THINKING_TOKENS` | Thinking budget (0=disable) | 31999 |

### MCP

| Variable | Purpose |
|----------|---------|
| `MCP_TIMEOUT` | Server startup timeout (ms) |
| `MCP_TOOL_TIMEOUT` | Tool execution timeout (ms) |
| `MAX_MCP_OUTPUT_TOKENS` | Max tool response tokens |
| `ENABLE_TOOL_SEARCH` | `auto`, `true`, `false` |

### UI & Session

| Variable | Purpose |
|----------|---------|
| `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` | Auto-compaction trigger % |
| `SLASH_COMMAND_TOOL_CHAR_BUDGET` | Skill metadata char limit |
| `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS` | Disable background tasks |
| `CLAUDE_CODE_TASK_LIST_ID` | Share task list across sessions |

### Network

| Variable | Purpose |
|----------|---------|
| `HTTP_PROXY` | HTTP proxy |
| `HTTPS_PROXY` | HTTPS proxy |
| `NO_PROXY` | Bypass proxy for domains |

---

## 17. Common Anti-Patterns

### 1. The Kitchen Sink Session

Problem: Start with one task, ask unrelated questions, go back. Context fills with noise.

**Fix**: `/clear` between unrelated tasks.

### 2. Correcting Over and Over

Problem: Claude does something wrong, you correct, still wrong, correct again. Context polluted.

**Fix**: After 2 failed corrections, `/clear` and write a better initial prompt.

### 3. Over-Specified CLAUDE.md

Problem: CLAUDE.md is too long, important rules get lost.

**Fix**: Ruthlessly prune. If Claude does it correctly without the instruction, delete it. Convert repeated behaviors to hooks instead.

### 4. Trust-Then-Verify Gap

Problem: Claude produces plausible-looking code that doesn't handle edge cases.

**Fix**: Always provide verification (tests, scripts, screenshots). If you can't verify it, don't ship it.

### 5. Infinite Exploration

Problem: "Investigate this" without scope. Claude reads hundreds of files.

**Fix**: Scope narrowly (`"check only src/auth/"`) or use subagents.

### 6. Ignoring Context Limits

Problem: Long sessions degrade Claude's performance.

**Fix**: Monitor context with `/context`. Use `/clear` proactively. Use subagents for exploration.

### 7. Not Using Verification

Problem: No tests, no linting, no type checking after changes.

**Fix**: Include verification in every prompt: "implement X, write tests, run them, fix failures."

### 8. Skipping Plan Mode

Problem: Jump straight to coding on complex tasks, solve wrong problem.

**Fix**: Use Plan Mode (`Ctrl+G`) for multi-file changes or unfamiliar code.

---

## 18. Resources and Community

### Official Documentation

- [Claude Code Docs](https://code.claude.com/docs/en/best-practices) — Anthropic official documentation
- [Claude Code Settings](https://code.claude.com/docs/en/settings) — Complete settings reference
- [Claude Code Hooks](https://code.claude.com/docs/en/hooks) — Hooks reference
- [Claude Code Skills](https://code.claude.com/docs/en/skills) — Skills documentation
- [Claude Code Subagents](https://code.claude.com/docs/en/sub-agents) — Custom subagents guide

### Community Resources

- [everything-claude-code](https://github.com/affaan-m/everything-claude-code) — Complete configuration collection (40.7k stars)
- [claude-code-showcase](https://github.com/ChrisWiles/claude-code-showcase) — Comprehensive example project
- [awesome-claude-code-subagents](https://github.com/VoltAgent/awesome-claude-code-subagents) — 100+ specialized subagents
- [anthropics/skills](https://github.com/anthropics/skills) — Official skills repository
- [claude-code-hooks-mastery](https://github.com/disler/claude-code-hooks-mastery) — Hooks examples

### Guides & Articles

- [The Complete Guide to CLAUDE.md](https://www.builder.io/blog/claude-md-guide) — Builder.io
- [How I Use Every Claude Code Feature](https://blog.sshh.io/p/how-i-use-every-claude-code-feature) — Power user deep dive
- [Writing a Good CLAUDE.md](https://www.humanlayer.dev/blog/writing-a-good-claude-md) — HumanLayer
- [Claude Code CLI Cheatsheet](https://shipyard.build/blog/claude-code-cheat-sheet/) — Shipyard
- [Claude Code Complete Guide](https://www.siddharthbharath.com/claude-code-the-complete-guide/) — Siddharth Bharath

### CLI Quick Reference

```bash
claude                          # Start interactive session
claude "query"                  # REPL with initial prompt
claude -p "query"               # Print mode (headless)
claude -c                       # Continue recent conversation
claude --resume                 # Select from recent sessions
claude mcp add <name>           # Add MCP server
claude mcp list                 # List MCP servers
claude update                   # Update Claude Code
claude --model claude-opus-4-6  # Use specific model
```

---

> **Last updated**: 2026-02-05
> **Claude Code version**: Latest
> **Sources**: Anthropic official docs, GitHub community, power user guides
