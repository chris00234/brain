# Claude Code 베스트 프랙티스 - 완벽 가이드

> 개발자가 Claude Code를 **100% 이상** 활용하기 위한 완벽 가이드 — CLI 뿐만 아니라 에이전트, 스킬, 훅, MCP 서버, 플러그인, CLAUDE.md, 설정, 워크플로우, SuperClaude 프레임워크까지 모든 생태계를 다룹니다.

---

## 목차

1. [[#1. Claude Code란 무엇인가]]
2. [[#2. 설정 계층 구조]]
3. [[#3. CLAUDE.md — 헌법]]
4. [[#4. 설정과 권한]]
5. [[#5. 커스텀 서브에이전트]]
6. [[#6. 스킬 (커스텀 슬래시 명령어)]]
7. [[#7. 훅 — 결정적 자동화]]
8. [[#8. MCP 서버 — 외부 도구 연동]]
9. [[#9. 플러그인 — 번들 확장]]
10. [[#10. 키보드 단축키와 CLI 플래그]]
11. [[#11. Claude Code를 위한 프롬프트 엔지니어링]]
12. [[#12. 컨텍스트 윈도우 관리]]
13. [[#13. 워크플로우 패턴]]
14. [[#14. 병렬 세션과 스케일링]]
15. [[#15. SuperClaude 프레임워크]]
16. [[#16. 환경 변수 레퍼런스]]
17. [[#17. 흔한 안티패턴]]
18. [[#18. 리소스와 커뮤니티]]

---

## 1. Claude Code란 무엇인가

Claude Code는 단순한 터미널 챗봇이 **아닙니다**. 다음을 수행할 수 있는 **완전한 에이전트 코딩 환경**입니다:

- 프로젝트 전체의 파일을 읽고, 쓰고, 편집
- 셸 명령어 실행 (빌드, 테스트, 배포)
- Grep, Glob, 고급 패턴 매칭으로 코드베이스 검색
- 웹 브라우징 및 문서 가져오기
- 격리된 컨텍스트에서 실행되는 전문 서브에이전트에 작업 위임
- MCP(Model Context Protocol)를 통한 외부 도구 연동
- CI/CD 파이프라인에서 헤드리스 모드로 실행
- 세션 간 영구 메모리 관리
- 여러 병렬 세션 조율 (에이전트 팀)

**핵심 사고 모델**: 원하는 것을 설명 → Claude가 구현 방법을 파악

### 사용 가능한 모델

| 모델 | ID | 최적 용도 |
|------|-----|---------|
| **Opus 4.6** | `claude-opus-4-6` | 가장 강력, 복잡한 아키텍처, 심층 분석 |
| **Sonnet 4.5** | `claude-sonnet-4-5-20250929` | 일상 코딩, 균형 잡힌 성능/속도 |
| **Haiku 4.5** | `claude-haiku-4-5-20251001` | 빠른 작업, 빠른 검색, 서브에이전트 탐색 |

모델 전환: 세션 내 `/model`, CLI에서 `--model`, 환경변수 `ANTHROPIC_MODEL`

---

## 2. 설정 계층 구조

Claude Code는 **4단계 스코프 시스템**을 사용합니다 (높은 우선순위 → 낮은 우선순위):

| 스코프 | 위치 | 대상 | 공유? |
|--------|------|------|------|
| **관리** | 시스템 레벨 `managed-settings.json` | 머신의 모든 사용자 | IT 배포 |
| **로컬** | `.claude/settings.local.json` | 현재 저장소만 | 아니오 (gitignored) |
| **프로젝트** | `.claude/settings.json` | 저장소 협업자 | 예 (커밋됨) |
| **사용자** | `~/.claude/settings.json` | 모든 프로젝트 | 아니오 |

### 관리 설정 시스템 경로

- **macOS**: `/Library/Application Support/ClaudeCode/`
- **Linux/WSL**: `/etc/claude-code/`

### 주요 설정 파일

| 파일 | 용도 |
|------|------|
| `~/.claude/settings.json` | 사용자 레벨 설정 (권한, 훅, 환경변수) |
| `.claude/settings.json` | 프로젝트 레벨 설정 (팀과 공유) |
| `.claude/settings.local.json` | 로컬 프로젝트 오버라이드 (gitignored) |
| `~/.claude.json` | 환경설정, OAuth, MCP 서버, 캐시 |
| `~/.claude/CLAUDE.md` | 모든 세션에 적용되는 글로벌 지침 |
| `./CLAUDE.md` | 프로젝트 레벨 지침 |
| `./CLAUDE.local.md` | 로컬 프로젝트 지침 (gitignored) |

---

## 3. CLAUDE.md — 헌법

**CLAUDE.md는 Claude Code를 효과적으로 사용하기 위한 가장 중요한 파일입니다.** Claude의 "헌법" — 모든 세션 시작 시 로드되는 영구 컨텍스트입니다.

### 시작하는 법

`/init` 명령으로 프로젝트 구조 기반의 초기 CLAUDE.md를 자동 생성하세요. 그리고 시간을 두고 개선하세요.

### 포함할 내용

```markdown
# 프로젝트: MyApp

## 빌드 & 테스트 명령어
- 빌드: `npm run build`
- 단일 테스트: `npm test -- --testPathPattern=<file>`
- 린트: `npm run lint`
- 타입 체크: `npx tsc --noEmit`

## 코드 스타일
- ES modules (import/export) 사용, CommonJS (require) 아님
- import 구조분해: `import { foo } from 'bar'`
- TypeScript strict 모드 사용

## 아키텍처
- 모노레포: apps/web, apps/api, packages/shared
- 상태 관리: Zustand (Redux 아님)
- API: tRPC + Zod 유효성 검사

## 워크플로우 규칙
- 코드 변경 후 반드시 타입체크 실행
- 전체 테스트 스위트 대신 개별 테스트 우선
- 기능 브랜치 사용, main에 직접 푸시 금지
- 커밋 메시지 형식: `type(scope): description`
```

### 포함하지 말아야 할 내용

- Claude가 코드를 읽으면 알 수 있는 것
- 표준 언어 규칙 (Claude가 이미 알고 있음)
- 상세한 API 문서 (링크만 제공)
- 자주 변경되는 정보
- 긴 설명이나 튜토리얼
- 린터/포매터로 처리해야 할 코드 스타일

### CLAUDE.md 위치 (모두 로드됨)

| 위치 | 사용 사례 |
|------|----------|
| `~/.claude/CLAUDE.md` | 모든 프로젝트의 글로벌 지침 |
| `./CLAUDE.md` | 프로젝트 전체 (git에 커밋) |
| `./CLAUDE.local.md` | 개인 오버라이드 (gitignore) |
| `./subdir/CLAUDE.md` | 하위 디렉토리별 (필요 시 로드) |

### Import 구문

```markdown
프로젝트 개요는 @README.md 참조.
Git 워크플로우: @docs/git-instructions.md
개인 오버라이드: @~/.claude/my-project-instructions.md
```

### 프로 팁

- **강조가 중요**: 중요한 규칙에는 "IMPORTANT" 또는 "YOU MUST" 사용
- **짧게 유지**: Claude가 규칙을 무시하면 CLAUDE.md가 너무 긴 것
- **코드처럼 관리**: 문제 발생 시 검토, 정기적으로 정리
- **변경 테스트**: 편집 후 Claude의 행동이 실제로 변하는지 관찰

---

## 4. 설정과 권한

### 권한 설정

```json
{
  "permissions": {
    "allow": [
      "Bash(npm run lint)",
      "Bash(npm run test *)",
      "Bash(git commit *)"
    ],
    "ask": [
      "Bash(git push *)"
    ],
    "deny": [
      "Bash(curl *)",
      "Read(./.env)",
      "Read(./.env.*)",
      "Read(./secrets/**)"
    ],
    "additionalDirectories": ["../docs/"],
    "defaultMode": "acceptEdits"
  }
}
```

### 권한 규칙 문법

| 규칙 | 매칭 대상 |
|------|----------|
| `Bash` | 모든 bash 명령어 |
| `Bash(npm run *)` | `npm run`으로 시작하는 명령어 |
| `Read(./.env)` | 특정 파일 읽기 |
| `Edit(./src/**)` | src/ 내 모든 파일 재귀적 편집 |
| `WebFetch(domain:example.com)` | 특정 도메인에서 가져오기 |
| `Task(agent-name)` | 특정 서브에이전트 |
| `Skill(skill-name)` | 특정 스킬 |

**평가 순서**: Deny > Ask > Allow (첫 번째 매칭 적용)

### 권한 모드

| 모드 | 동작 |
|------|------|
| `default` | 표준 권한 확인 (프롬프트 표시) |
| `acceptEdits` | 파일 편집 자동 수락 |
| `dontAsk` | 권한 요청 자동 거부 (허용된 도구는 동작) |
| `bypassPermissions` | 모든 권한 검사 건너뛰기 |
| `plan` | 플랜 모드 (읽기 전용 탐색) |

---

## 5. 커스텀 서브에이전트

서브에이전트는 자체 컨텍스트 윈도우에서 커스텀 시스템 프롬프트, 특정 도구 접근권, 독립적 권한으로 실행되는 **전문 AI 어시스턴트**입니다.

### 왜 서브에이전트를 사용하나?

- **컨텍스트 보존**: 탐색이 메인 대화를 오염시키지 않음
- **제약 적용**: 서브에이전트가 사용할 수 있는 도구 제한
- **전문화**: 특정 도메인에 집중된 시스템 프롬프트
- **비용 제어**: Haiku 같은 저렴한 모델로 작업 라우팅

### 내장 서브에이전트

| 에이전트 | 모델 | 도구 | 용도 |
|---------|------|------|------|
| **Explore** | Haiku | 읽기 전용 | 빠른 코드베이스 검색/탐색 |
| **Plan** | 상속 | 읽기 전용 | 플랜 모드 리서치 |
| **General-purpose** | 상속 | 모두 | 복잡한 다단계 작업 |
| **Bash** | 상속 | Bash | 별도 컨텍스트의 터미널 명령어 |

### 커스텀 서브에이전트 만들기

**위치**: `~/.claude/agents/` (사용자 레벨) 또는 `.claude/agents/` (프로젝트 레벨)

**인터랙티브**: `/agents` 실행 → 새 에이전트 생성

**수동 생성**: `.md` 파일 작성:

```markdown
---
name: security-reviewer
description: 코드의 보안 취약점을 검토합니다. 코드 변경 후 능동적으로 사용하세요.
tools: Read, Grep, Glob, Bash
model: opus
---

당신은 시니어 보안 엔지니어입니다. 코드 검토 항목:
- 인젝션 취약점 (SQL, XSS, 명령어 인젝션)
- 인증 및 권한 부여 결함
- 코드 내 비밀 또는 자격증명
- 불안전한 데이터 처리

구체적인 라인 참조와 수정 제안을 제공하세요.
```

### 프론트매터 필드

| 필드 | 필수 | 설명 |
|------|------|------|
| `name` | 예 | 고유 식별자 (소문자, 하이픈) |
| `description` | 예 | Claude가 위임 시기를 판단하는 설명 |
| `tools` | 아니오 | 허용된 도구 (생략 시 모두 상속) |
| `disallowedTools` | 아니오 | 거부할 도구 |
| `model` | 아니오 | `sonnet`, `opus`, `haiku`, 또는 `inherit` |
| `permissionMode` | 아니오 | 권한 모드 |
| `skills` | 아니오 | 시작 시 컨텍스트에 미리 로드할 스킬 |
| `hooks` | 아니오 | 서브에이전트 범위의 라이프사이클 훅 |
| `memory` | 아니오 | 영구 메모리: `user`, `project`, `local` |

### 영구 메모리

```markdown
---
name: code-reviewer
description: 코드 품질과 모범 사례를 검토합니다
memory: user
---
```

| 범위 | 위치 | 사용 시기 |
|------|------|----------|
| `user` | `~/.claude/agent-memory/<name>/` | 모든 프로젝트에서의 학습 |
| `project` | `.claude/agent-memory/<name>/` | 프로젝트별, git으로 공유 |
| `local` | `.claude/agent-memory-local/<name>/` | 프로젝트별, 커밋 안 함 |

---

## 6. 스킬 (커스텀 슬래시 명령어)

스킬은 도메인 지식과 재사용 가능한 워크플로우로 Claude의 기능을 확장합니다. `.claude/skills/<name>/SKILL.md` 파일에 YAML 프론트매터로 작성합니다.

### 스킬 만들기

```bash
mkdir -p .claude/skills/fix-issue
```

```markdown
# .claude/skills/fix-issue/SKILL.md
---
name: fix-issue
description: GitHub 이슈 번호로 이슈 수정
disable-model-invocation: true
---

GitHub 이슈 $ARGUMENTS 수정:

1. `gh issue view $ARGUMENTS`로 상세 정보 확인
2. 문제 이해
3. 관련 파일 코드베이스 검색
4. 수정 구현
5. 테스트 작성 및 실행
6. 의미있는 커밋 생성
7. 푸시 및 PR 생성
```

호출: `/fix-issue 1234`

### 스킬 위치

| 위치 | 범위 |
|------|------|
| `~/.claude/skills/<name>/SKILL.md` | 모든 프로젝트 (개인) |
| `.claude/skills/<name>/SKILL.md` | 이 프로젝트만 |
| 플러그인 `skills/<name>/SKILL.md` | 플러그인 활성화된 곳 |

### 프론트매터 필드

| 필드 | 설명 |
|------|------|
| `name` | 표시 이름 / 슬래시 명령어 이름 |
| `description` | Claude가 사용할 시기 (구체적으로 작성!) |
| `argument-hint` | 자동완성 힌트: `[issue-number]` |
| `disable-model-invocation` | `true` = 사용자만 호출 가능 (배포, 커밋 등) |
| `user-invocable` | `false` = Claude만 호출 가능 (배경 지식) |
| `allowed-tools` | 스킬 활성 시 사용 가능한 도구 |
| `model` | 사용할 모델 |
| `context` | `fork` = 격리된 서브에이전트 컨텍스트에서 실행 |
| `agent` | `context: fork` 시 사용할 서브에이전트 |

### 변수 치환

| 변수 | 설명 |
|------|------|
| `$ARGUMENTS` | 스킬에 전달된 모든 인자 |
| `$ARGUMENTS[0]` / `$0` | 첫 번째 인자 |
| `$ARGUMENTS[1]` / `$1` | 두 번째 인자 |
| `${CLAUDE_SESSION_ID}` | 현재 세션 ID |

### 동적 컨텍스트 주입

`` !`command` `` 구문으로 셸 명령어를 Claude에게 보내기 전에 실행:

```markdown
---
name: pr-summary
description: 현재 PR 요약
context: fork
agent: Explore
---

## PR 컨텍스트
- 변경사항: !`gh pr diff`
- 코멘트: !`gh pr view --comments`
- 변경 파일: !`gh pr diff --name-only`

이 풀 리퀘스트를 요약하세요.
```

---

## 7. 훅 — 결정적 자동화

훅은 Claude 라이프사이클의 특정 시점에서 자동으로 실행되는 **셸 명령어 또는 LLM 프롬프트**입니다. CLAUDE.md 지침(권고적)과 달리, 훅은 **결정적** — 액션이 반드시 실행됨을 보장합니다.

### 훅 이벤트 (라이프사이클)

| 이벤트 | 시점 | 차단 가능? |
|--------|------|-----------|
| `SessionStart` | 세션 시작/재개 | 아니오 |
| `UserPromptSubmit` | 사용자 프롬프트 제출, 처리 전 | 예 |
| `PreToolUse` | 도구 호출 실행 전 | 예 |
| `PermissionRequest` | 권한 대화상자 표시 | 예 |
| `PostToolUse` | 도구 호출 성공 후 | 아니오 |
| `PostToolUseFailure` | 도구 호출 실패 후 | 아니오 |
| `Notification` | 알림 전송 | 아니오 |
| `SubagentStart` | 서브에이전트 생성 | 아니오 |
| `SubagentStop` | 서브에이전트 종료 | 예 |
| `Stop` | Claude 응답 완료 | 예 |
| `PreCompact` | 컨텍스트 압축 전 | 아니오 |
| `SessionEnd` | 세션 종료 | 아니오 |

### 설정 예시

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

### 훅 유형

| 유형 | 설명 |
|------|------|
| `command` | 셸 명령어 실행 (stdin으로 JSON 수신) |
| `prompt` | LLM에 프롬프트 전송, 예/아니오 평가 |
| `agent` | 도구(Read, Grep, Glob) 사용 가능한 서브에이전트 생성 |

### 종료 코드

| 코드 | 의미 |
|------|------|
| **0** | 성공 — 액션 허용, stdout에서 JSON 파싱 |
| **2** | 차단 오류 — 액션 차단, stderr을 Claude에게 표시 |
| **기타** | 비차단 오류 — 계속 진행, verbose 모드에서 stderr 표시 |

### 프롬프트 기반 훅

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "prompt",
            "prompt": "모든 작업이 완료되었는지 평가: $ARGUMENTS. 테스트 통과 확인.",
            "timeout": 30
          }
        ]
      }
    ]
  }
}
```

### 비동기 훅

`"async": true`로 백그라운드 실행:

```json
{
  "type": "command",
  "command": ".claude/hooks/run-tests.sh",
  "async": true,
  "timeout": 300
}
```

---

## 8. MCP 서버 — 외부 도구 연동

MCP(Model Context Protocol) 서버는 Claude가 외부 서비스(데이터베이스, API, 디자인 도구, 브라우저 등)와 상호작용할 수 있게 합니다.

### 설정

MCP 서버는 `~/.claude.json` (사용자 레벨) 또는 `.mcp.json` (프로젝트 레벨)에 설정:

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

### MCP 서버 관리

```bash
# CLI로 추가
claude mcp add my-server -e API_KEY=123 -- npx @some/mcp-server

# 설정된 서버 목록
claude mcp list

# 서버 제거
claude mcp remove my-server
```

### 인기 MCP 서버

| 서버 | 용도 | 패키지 |
|------|------|--------|
| **Context7** | 라이브러리 문서 조회 | `@upstash/context7-mcp` |
| **Playwright** | 브라우저 자동화 & E2E 테스트 | `@executeautomation/playwright-mcp-server` |
| **Sequential Thinking** | 다단계 추론 | `@modelcontextprotocol/server-sequential-thinking` |
| **Magic (21st.dev)** | UI 컴포넌트 생성 | `@21st-dev/magic@latest` |
| **Serena** | 의미론적 코드 이해 | `serena` (uvx 경유) |
| **Morphllm** | 대량 코드 변환 | `@morph-llm/morph-fast-apply` |
| **GitHub** | GitHub API 연동 | `@modelcontextprotocol/server-github` |
| **Obsidian** | Obsidian 볼트 접근 | `@mauricio.wolff/mcp-obsidian@latest` |

### MCP 도구 명명 규칙

도구 형식: `mcp__<서버>__<도구>`

예시:
- `mcp__memory__create_entities`
- `mcp__filesystem__read_file`
- `mcp__github__search_repositories`

---

## 9. 플러그인 — 번들 확장

플러그인은 스킬, 훅, 서브에이전트, MCP 서버를 하나의 설치 가능한 단위로 묶습니다.

### 플러그인 설치

```
# 마켓플레이스 탐색
/plugin

# 마켓플레이스에서 설치
/plugin marketplace add owner/repo
/plugin install plugin-name@marketplace-name
```

### 플러그인 구조

```
.claude-plugin/
├── plugin.json        # 플러그인 매니페스트
├── commands/          # 슬래시 명령어
├── agents/            # 서브에이전트
├── skills/            # 스킬
├── hooks/hooks.json   # 훅 (v2.1+에서 자동 로드)
├── .mcp.json          # MCP 서버 설정
└── README.md
```

### 주목할만한 커뮤니티 플러그인

- **everything-claude-code** (40.7k 스타): 에이전트, 스킬, 훅, 명령어, 규칙의 완전한 설정 컬렉션
- **claude-code-showcase**: 훅, 스킬, 에이전트, 명령어, GitHub Actions 포함 종합 예제

---

## 10. 키보드 단축키와 CLI 플래그

### 세션 내 단축키

| 단축키 | 동작 |
|--------|------|
| `Esc` | Claude 중간 중지 (컨텍스트 유지) |
| `Esc + Esc` | 되감기/체크포인트 메뉴 열기 |
| `Ctrl+G` | 에디터에서 플랜 열기 |
| `Ctrl+O` | 상세 모드 토글 (사고 과정 보기) |
| `Ctrl+B` | 실행 중인 작업 백그라운드로 |
| `Option+T` / `Alt+T` | 확장 사고 토글 |
| `Shift+Enter` | 여러 줄 입력 |
| `!` | 셸 모드 진입 |

### 슬래시 명령어

| 명령어 | 용도 |
|--------|------|
| `/help` | 모든 명령어 표시 |
| `/init` | CLAUDE.md 초기 생성 |
| `/config` | 인터랙티브 설정 |
| `/permissions` | 권한 관리 |
| `/hooks` | 훅 설정 |
| `/agents` | 서브에이전트 관리 |
| `/mcp` | MCP 서버 관리 |
| `/model` | 모델 전환 |
| `/compact` | 컨텍스트 압축 |
| `/clear` | 컨텍스트 윈도우 초기화 |
| `/rewind` | 이전 체크포인트 복원 |
| `/rename` | 현재 세션 이름 지정 |
| `/context` | 컨텍스트 사용량 표시 |
| `/plugin` | 플러그인 마켓플레이스 탐색 |
| `/vim` | vim 편집 모드 활성화 |
| `/sandbox` | OS 레벨 샌드박싱 활성화 |

### CLI 플래그

| 플래그 | 용도 | 예시 |
|--------|------|------|
| `--continue` / `-c` | 최근 대화 재개 | `claude -c` |
| `--resume` / `-r` | 최근 세션에서 선택 | `claude --resume` |
| `-p "prompt"` | 프린트 모드 (헤드리스) | `claude -p "설명해줘"` |
| `--model` | 모델 지정 | `--model claude-opus-4-6` |
| `--add-dir` | 추가 디렉토리 포함 | `--add-dir ../lib ../docs` |
| `--allowedTools` | 특정 도구 허용 | `--allowedTools "Write" "Bash(git *)"` |
| `--disallowedTools` | 특정 도구 차단 | `--disallowedTools "Bash(rm *)"` |
| `--max-turns` | 대화 라운드 제한 | `--max-turns 5` |
| `--output-format` | 응답 형식 | `--output-format json` |
| `--verbose` | 상세 로깅 | `--verbose` |
| `--agents` | 임시 에이전트 JSON 전달 | `--agents '{...}'` |
| `--dangerously-skip-permissions` | 모든 권한 검사 건너뛰기 | 샌드박스에서만! |

---

## 11. Claude Code를 위한 프롬프트 엔지니어링

### 가장 중요한 실천법

**Claude가 자기 작업을 검증할 수 있게 하세요.** 이것이 가장 효과적인 방법입니다.

| 나쁜 예 | 좋은 예 |
|---------|---------|
| "이메일 유효성 검사 구현해줘" | "validateEmail 함수 작성. 테스트 케이스: user@example.com = true, invalid = false, user@.com = false. 구현 후 테스트 실행" |
| "대시보드 더 좋게 만들어" | "[스크린샷 붙여넣기] 이 디자인 구현. 결과 스크린샷 찍어서 원본과 비교" |
| "빌드가 실패해" | "이 에러로 빌드 실패: [에러 붙여넣기]. 수정하고 빌드 성공 확인" |

### 컨텍스트 제공

- **`@` 파일 참조**: `@./src/auth/login.ts` (Claude가 파일을 읽음)
- **이미지 붙여넣기**: 스크린샷을 직접 드래그앤드롭
- **URL 제공**: 문서 링크 공유
- **데이터 파이프**: `cat error.log | claude`
- **Claude가 직접 가져오게**: Claude에게 도구를 통해 컨텍스트를 직접 가져오라고 지시

### Claude에게 인터뷰 받기

큰 기능의 경우:

```
[간단한 설명]을 만들고 싶어. AskUserQuestion 도구를 사용해서 자세히 인터뷰해줘.

기술 구현, UI/UX, 엣지 케이스, 트레이드오프에 대해 물어봐.
모든 것을 다룰 때까지 인터뷰 계속해, 그리고 완전한 스펙을 SPEC.md에 작성해.
```

그런 다음 **새 세션**에서 스펙 기반으로 구현.

---

## 12. 컨텍스트 윈도우 관리

> **컨텍스트는 가장 중요한 리소스입니다.** 채워질수록 성능이 저하됩니다.

### 컨텍스트 모니터링

- `/context`로 사용량 확인
- 커스텀 상태 라인으로 토큰 사용량 표시
- 자동 압축 경고 주시

### 전략

| 전략 | 방법 |
|------|------|
| 작업 간 `/clear` | 관련 없는 작업에 대해 컨텍스트 초기화 |
| `/compact <지침>` | 초점을 맞춰 요약: `/compact API 변경에 집중` |
| 리서치에 서브에이전트 사용 | 별도 컨텍스트에서 탐색, 요약만 반환 |
| 조사 범위 한정 | "코드베이스 조사해줘" 대신 "src/auth/만 확인해줘" |
| 새 세션 + 더 나은 프롬프트 | 2회 이상 수정 실패 후 깨끗하게 시작 |

### 세션 관리

```bash
claude --continue    # 가장 최근 대화 재개
claude --resume      # 최근 세션에서 선택
```

`/rename`으로 세션 이름 지정: "oauth-마이그레이션", "메모리-누수-디버깅"

---

## 13. 워크플로우 패턴

### 표준 워크플로우

```
1. 탐색    → 플랜 모드: 파일 읽기, 코드 이해
2. 계획    → Claude에게 구현 계획 생성 요청
3. 구현    → 노멀 모드로 전환, 검증과 함께 실행
4. 커밋    → Claude에게 설명적 커밋 메시지로 커밋 및 PR 요청
```

### 작성자/검토자 패턴 (두 세션)

| 세션 A (작성자) | 세션 B (검토자) |
|----------------|----------------|
| "API에 대한 레이트 리미터 구현" | |
| | "@src/middleware/rateLimiter.ts의 레이트 리미터 검토. 엣지 케이스, 레이스 컨디션 확인" |
| "검토 피드백: [붙여넣기]. 이 문제들 해결" | |

### 테스트 우선 패턴

```
이 요구사항에 기반한 OAuth 콜백 핸들러 테스트 작성: [요구사항].
핸들러를 아직 구현하지 말고 테스트만.
```

새 세션에서:
```
OAuth 콜백 핸들러 구현. @tests/auth.test.ts의 모든 테스트를 통과시켜.
```

### 팬아웃 패턴 (배치 처리)

```bash
# 작업 목록 생성
claude -p "마이그레이션 필요한 모든 Python 파일 나열" > files.txt

# 각 파일 병렬 처리
for file in $(cat files.txt); do
  claude -p "$file를 React에서 Vue로 마이그레이션. OK 또는 FAIL 반환." \
    --allowedTools "Edit,Bash(git commit *)" &
done
wait
```

---

## 14. 병렬 세션과 스케일링

### 다중 세션

- **Claude Desktop**: 여러 로컬 세션을 시각적으로 관리
- **웹의 Claude Code**: 클라우드 인프라, 격리된 VM
- **에이전트 팀**: 공유 작업과 메시징으로 자동 조율

### 헤드리스 모드

```bash
# 일회성 쿼리
claude -p "이 프로젝트가 뭘 하는지 설명해"

# 구조화된 출력
claude -p "모든 API 엔드포인트 나열" --output-format json

# 실시간 스트리밍
claude -p "이 로그 파일 분석" --output-format stream-json
```

---

## 15. SuperClaude 프레임워크

SuperClaude는 구조화된 행동 모드, MCP 서버 오케스트레이션, 작업 관리 패턴을 추가하는 Claude Code의 **프레임워크 확장**입니다.

### SuperClaude가 제공하는 것

현재 SuperClaude 설정은 `~/.claude/CLAUDE.md`를 통해 로드되는 다음 구성요소를 포함합니다:

#### 핵심 프레임워크 파일

| 파일 | 용도 |
|------|------|
| `FLAGS.md` | 행동 플래그 (`--think`, `--ultrathink`, `--brainstorm` 등) |
| `PRINCIPLES.md` | 소프트웨어 엔지니어링 원칙 (SOLID, DRY, KISS, YAGNI) |
| `RULES.md` | 우선순위 시스템의 실행 가능한 행동 규칙 |

#### 행동 모드

| 모드 | 트리거 | 용도 |
|------|--------|------|
| **브레인스토밍** | 모호한 요청, "아마도", "생각 중" | 소크라테스식 대화를 통한 협업 발견 |
| **내성** | 자기 분석, 오류 복구 | 투명성 마커를 사용한 메타인지 분석 |
| **오케스트레이션** | 멀티 도구 작업, 성능 제약 | 지능적 도구 선택과 병렬 실행 |
| **작업 관리** | 3단계 이상, 복잡한 범위 | 메모리를 사용한 계층적 작업 조직 |
| **토큰 효율** | 컨텍스트 >75%, `--uc` 플래그 | 심볼 강화 커뮤니케이션, 30-50% 감소 |
| **비즈니스 패널** | 비즈니스 분석, 전략 | 멀티 전문가 패널 (Porter, Christensen, Drucker 등) |

### SuperClaude 플래그

| 플래그 | 용도 |
|--------|------|
| `--brainstorm` | 협업 발견 마인드셋 활성화 |
| `--introspect` | 사고 과정을 마커와 함께 노출 |
| `--task-manage` | 계층적 작업 조직 |
| `--orchestrate` | 도구 선택 최적화 |
| `--token-efficient` / `--uc` | 심볼 강화 커뮤니케이션 |
| `--think` | 표준 구조화 분석 (~4K 토큰) |
| `--think-hard` | 심층 분석 (~10K 토큰) |
| `--ultrathink` | 최대 깊이 (~32K 토큰) |
| `--c7` | Context7 (문서) 활성화 |
| `--seq` | Sequential (추론) 활성화 |
| `--magic` | Magic (UI) 활성화 |
| `--morph` | Morphllm (대량 편집) 활성화 |
| `--serena` | Serena (의미론적 이해) 활성화 |
| `--play` | Playwright (브라우저) 활성화 |
| `--all-mcp` | 모든 MCP 서버 활성화 |
| `--no-mcp` | 모든 MCP 서버 비활성화 |

### SuperClaude 스킬 (커스텀 명령어)

| 스킬 | 용도 |
|------|------|
| `/sc:load` | 프로젝트 컨텍스트로 세션 초기화 |
| `/sc:save` | 세션 컨텍스트와 메모리 저장 |
| `/sc:analyze` | 종합 코드 분석 |
| `/sc:implement` | 페르소나 활성화로 기능 구현 |
| `/sc:improve` | 체계적 코드 개선 |
| `/sc:explain` | 코드와 개념의 명확한 설명 |
| `/sc:troubleshoot` | 문제 진단 및 해결 |
| `/sc:test` | 커버리지 분석으로 테스트 실행 |
| `/sc:build` | 에러 처리로 빌드/컴파일 |
| `/sc:design` | 시스템 아키텍처 및 API 설계 |
| `/sc:cleanup` | 죽은 코드 제거, 구조 최적화 |
| `/sc:git` | 지능적 커밋으로 Git 작업 |
| `/sc:task` | 복잡한 작업 실행 및 위임 |
| `/sc:brainstorm` | 인터랙티브 요구사항 발견 |
| `/sc:business-panel` | 멀티 전문가 비즈니스 분석 |
| `/sc:help` | 모든 /sc 명령어 나열 |

### SuperClaude 에이전트

| 에이전트 | 용도 |
|---------|------|
| `backend-architect` | 신뢰할 수 있는 백엔드 시스템 설계 |
| `frontend-architect` | 접근성 있고 성능 좋은 UI |
| `system-architect` | 확장 가능한 시스템 아키텍처 |
| `security-engineer` | 보안 취약점 및 컴플라이언스 |
| `performance-engineer` | 측정 기반 최적화 |
| `quality-engineer` | 테스트 전략 및 엣지 케이스 |
| `devops-architect` | 인프라 및 배포 자동화 |
| `python-expert` | 프로덕션 레디 Python 코드 |
| `refactoring-expert` | 체계적 리팩토링으로 코드 품질 |
| `technical-writer` | 명확한 기술 문서 |
| `requirements-analyst` | 아이디어를 명세로 변환 |
| `root-cause-analyst` | 증거 기반 문제 조사 |
| `socratic-mentor` | 질문을 통한 프로그래밍 교육 |
| `learning-guide` | 개념 교육 및 코드 설명 |
| `business-panel-experts` | 멀티 전문가 비즈니스 전략 |

---

## 16. 환경 변수 레퍼런스

### 인증 & API

| 변수 | 용도 |
|------|------|
| `ANTHROPIC_API_KEY` | API 키 |
| `ANTHROPIC_MODEL` | 모델 오버라이드 |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | Haiku 모델 오버라이드 |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | Sonnet 모델 오버라이드 |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` | Opus 모델 오버라이드 |

### 도구 & Bash

| 변수 | 용도 |
|------|------|
| `BASH_DEFAULT_TIMEOUT_MS` | 기본 bash 타임아웃 |
| `BASH_MAX_TIMEOUT_MS` | 최대 bash 타임아웃 |
| `CLAUDE_CODE_SHELL` | 셸 오버라이드 |

### 모델 & 성능

| 변수 | 용도 | 기본값 |
|------|------|--------|
| `CLAUDE_CODE_MAX_OUTPUT_TOKENS` | 최대 출력 (1-64000) | 32000 |
| `CLAUDE_CODE_EFFORT_LEVEL` | `low`, `medium`, `high` | `high` |
| `MAX_THINKING_TOKENS` | 사고 예산 (0=비활성화) | 31999 |

### MCP

| 변수 | 용도 |
|------|------|
| `MCP_TIMEOUT` | 서버 시작 타임아웃 (ms) |
| `MCP_TOOL_TIMEOUT` | 도구 실행 타임아웃 (ms) |
| `MAX_MCP_OUTPUT_TOKENS` | 최대 도구 응답 토큰 |

---

## 17. 흔한 안티패턴

### 1. 잡탕 세션

문제: 하나의 작업으로 시작 → 관련 없는 질문 → 다시 첫 작업. 컨텍스트가 잡음으로 가득.

**해결**: 관련 없는 작업 사이에 `/clear`.

### 2. 반복적 수정

문제: Claude 오류 → 수정 요청 → 여전히 오류 → 다시 수정 요청. 컨텍스트 오염.

**해결**: 2번 수정 실패 후 `/clear`하고 더 나은 초기 프롬프트 작성.

### 3. 과도한 CLAUDE.md

문제: CLAUDE.md가 너무 길어 중요한 규칙이 묻힘.

**해결**: 과감하게 정리. 지침 없이도 Claude가 올바르게 하면 삭제. 반복적 행동은 훅으로 전환.

### 4. 신뢰 후 검증 격차

문제: 그럴듯해 보이지만 엣지 케이스를 처리하지 않는 코드.

**해결**: 항상 검증 제공 (테스트, 스크립트, 스크린샷). 검증할 수 없으면 배포하지 마세요.

### 5. 무한 탐색

문제: 범위 없이 "이것 조사해줘". Claude가 수백 개 파일 읽음.

**해결**: 범위 좁히기 (`"src/auth/만 확인해줘"`) 또는 서브에이전트 사용.

### 6. 컨텍스트 제한 무시

문제: 긴 세션이 Claude 성능을 저하.

**해결**: `/context`로 모니터링. 능동적으로 `/clear`. 탐색에는 서브에이전트 사용.

### 7. 검증 미사용

문제: 변경 후 테스트, 린팅, 타입 체크 없음.

**해결**: 모든 프롬프트에 검증 포함: "X 구현하고 테스트 작성, 실행, 실패 수정."

### 8. 플랜 모드 건너뛰기

문제: 복잡한 작업에서 바로 코딩 시작, 잘못된 문제 해결.

**해결**: 여러 파일 변경이나 익숙하지 않은 코드에는 플랜 모드 (`Ctrl+G`) 사용.

---

## 18. 리소스와 커뮤니티

### 공식 문서

- [Claude Code Docs](https://code.claude.com/docs/en/best-practices) — Anthropic 공식 문서
- [Claude Code Settings](https://code.claude.com/docs/en/settings) — 전체 설정 레퍼런스
- [Claude Code Hooks](https://code.claude.com/docs/en/hooks) — 훅 레퍼런스
- [Claude Code Skills](https://code.claude.com/docs/en/skills) — 스킬 문서
- [Claude Code Subagents](https://code.claude.com/docs/en/sub-agents) — 커스텀 서브에이전트 가이드

### 커뮤니티 리소스

- [everything-claude-code](https://github.com/affaan-m/everything-claude-code) — 완전한 설정 컬렉션 (40.7k 스타)
- [claude-code-showcase](https://github.com/ChrisWiles/claude-code-showcase) — 종합 예제 프로젝트
- [awesome-claude-code-subagents](https://github.com/VoltAgent/awesome-claude-code-subagents) — 100개 이상의 전문 서브에이전트
- [anthropics/skills](https://github.com/anthropics/skills) — 공식 스킬 리포지토리

### 가이드 & 아티클

- [The Complete Guide to CLAUDE.md](https://www.builder.io/blog/claude-md-guide) — Builder.io
- [How I Use Every Claude Code Feature](https://blog.sshh.io/p/how-i-use-every-claude-code-feature) — 파워 유저 딥 다이브
- [Claude Code CLI Cheatsheet](https://shipyard.build/blog/claude-code-cheat-sheet/) — Shipyard

### CLI 빠른 참조

```bash
claude                          # 인터랙티브 세션 시작
claude "쿼리"                    # 초기 프롬프트로 REPL
claude -p "쿼리"                 # 프린트 모드 (헤드리스)
claude -c                       # 최근 대화 재개
claude --resume                 # 최근 세션에서 선택
claude mcp add <name>           # MCP 서버 추가
claude mcp list                 # MCP 서버 목록
claude update                   # Claude Code 업데이트
claude --model claude-opus-4-6  # 특정 모델 사용
```

---

> **마지막 업데이트**: 2026-02-05
> **Claude Code 버전**: 최신
> **출처**: Anthropic 공식 문서, GitHub 커뮤니티, 파워 유저 가이드
