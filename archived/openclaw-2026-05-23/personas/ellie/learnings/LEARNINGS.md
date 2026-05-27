# LEARNINGS

## [LRN-20260227-001] correction

**Logged**: 2026-02-27T16:44:00-08:00
**Priority**: high
**Status**: pending
**Area**: infra

### Summary
Repeated gateway restarts and mixed OpenClaw entrypoints caused avoidable downtime and response disruption.

### Details
User reported gateway went down multiple times during tuning. Root pattern:
- Multiple config changes were applied with immediate stop/start cycles.
- CLI entrypoint alternated between Homebrew install (`/opt/homebrew/.../openclaw`) and local source checkout (`/Users/chrischo/openclaw/...`), causing service command path flips.
- This created service churn and transient channel unavailability.

### Suggested Action
- Use one canonical CLI path for service operations (Homebrew) and avoid mixed entrypoints.
- Batch config edits and perform a single planned restart.
- Require explicit user confirmation before non-emergency gateway restart.
- Post-change verification checklist: `gateway status` + `config.get` + quick channel probe.

### Metadata
- Source: user_feedback
- Related Files: /Users/chrischo/.openclaw/openclaw.json
- Tags: gateway, restart, stability, process

---

## [LRN-20260228-002] correction

**Logged**: 2026-02-28T20:34:00-08:00
**Priority**: critical
**Status**: pending
**Area**: infra

### Summary
Jenna Tesla 장애 대응 중 live 세션 정리 + 잘못된 tool policy 변경(`allow` 사용)으로 사용자 응답 경로가 꼬이고 "권한 없음"/혼선 메시지가 노출되었다.

### Details
재현된 문제 패턴:
- `agents.jenna.tools.allow`를 additive 용도로 사용함.
- OpenClaw에서 `allow`는 사실상 엄격 allowlist로 작동해 프로필 기반 도구 구성을 예기치 않게 축소할 수 있음.
- 동시에 active direct DM 세션을 정리하면서 in-flight 컨텍스트와 inter-session 디버깅 문맥이 사용자 대화에 섞임.
- 결과적으로 Jenna가 실제 Tesla 실행 대신 혼합 문맥 응답(권한/경로 혼선)을 반환.

### Suggested Action
- **규칙 1**: 권한 "추가"는 `alsoAllow`만 사용 (`allow` 금지).
- **규칙 2**: active `agent:*:telegram:*:direct:<user>` 세션은 유지보수 창/명시 승인 없이 삭제·리셋 금지.
- **규칙 3**: 정책 변경 후 운영 반영 전 canary 3종 필수
  1) `exec echo` 성공
  2) `read` 성공
  3) 대상 skill 최소 1회 실동작 확인
- **규칙 4**: 복구 중 사용자 요청은 Ellie direct 실행으로 우선 처리하고, Jenna는 안정화 후 재투입.

### Metadata
- Source: user_feedback
- Related Files: /Users/chrischo/.openclaw/openclaw.json, /Users/chrischo/.openclaw/agents/jenna/sessions/sessions.json
- Tags: sessions, tool-policy, jenna, tesla, regression-prevention
- See Also: LRN-20260227-001

---

## [LRN-20260401-004] correction — 핸드오프 완료 시 중간 업데이트 누락

**Logged**: 2026-04-01T18:30:00-07:00
**Priority**: high
**Status**: applied
**Area**: behavior

### Summary
Liz 핸드오프 결과 수신 후 Chris에게 중간 업데이트 없이 바로 인프라 작업에 돌입. "어떻게 되어가?" 질문 2회를 받고 나서야 상황 공유.

### Details
- Liz가 MCC basePath 코드 변경 완료 → sessions_send로 결과 수신
- Ellie가 바로 Docker rebuild + nginx + Homer 작업 시작
- Chris에게 "Liz 완료, 내가 인프라 반영 시작한다" 업데이트를 안 보냄
- Chris가 2번 물어본 후에야 상황 공유

### Suggested Action
- **규칙**: 핸드오프 결과 수신 시 **3단계 업데이트 필수**:
  1. 핸드오프 결과 수신 즉시 → "Liz 완료, 인프라 반영 시작"
  2. 주요 마일스톤(빌드 완료 등) → 진행 상황
  3. 전체 완료 → 최종 검증 결과
- 60초 이상 작업이면 시작 시점에 반드시 한 줄 업데이트

### Metadata
- Source: user_feedback
- Tags: handoff, communication, update-cadence, improvement
- **2026-04-01 18:42 재발**: 학습 기록 직후에도 동일 패턴 반복. 기록 ≠ 학습. 행동 체크리스트로 승격 필요.

### 행동 체크리스트 (매 핸드오프/큰 작업 전 반드시 확인)
- [ ] Chris 의도를 내 말로 재확인했는가?
- [ ] 모호한 부분 있으면 작업 전에 물어봤는가?
- [ ] 핸드오프 보내기 전에 Chris에게 계획 공유했는가?
- [ ] 결과 수신 즉시 Chris에게 업데이트했는가?
- **3회 연속 위반 (18:17, 18:45, 18:46)** — 기록이 아니라 응답 파이프라인에 강제 삽입 필요. 모든 [DONE] 수신 → 첫 액션은 Chris 메시지. 예외 없음.

---

## [LRN-20260401-005] discovery — Cloudflare API 토큰 vs Global API Key

**Logged**: 2026-04-01T18:30:00-07:00
**Priority**: medium
**Status**: applied
**Area**: infra

### Summary
`CLOUDFLARE_API_TOKEN` 환경변수에 저장된 값이 실제로는 Global API Key (37자)였으나 Bearer 토큰(40자)으로 가정하고 반복 실패.

### Details
- Bearer Authorization 인증 5회 실패 후에야 Global API Key 방식 시도
- 길이 37자 → 정상 Global API Key, `X-Auth-Email + X-Auth-Key` 방식으로 인증 성공

### Suggested Action
- Cloudflare API 호출 시 토큰 길이/형식 먼저 확인:
  - 40자 hex = API Token → Bearer
  - 37자 hex = Global API Key → X-Auth-Email + X-Auth-Key
- TOOLS.md에 인증 방식 기록해둘 것

### Metadata
- Source: discovery
- Tags: cloudflare, api-auth, troubleshooting

---

## [LRN-20260307-003] workflow preference

**Logged**: 2026-03-07T16:31:00-08:00
**Priority**: high
**Status**: pending
**Area**: behavior

### Summary
사용자는 Ellie가 매 프롬프트마다 자가 학습/개선 루프를 더 강하게 적용하길 원함.

### Details
직접 피드백: "너가 자가 학습이 완벽해져서 더 개선이 되야해 매 프롬프트마다"

의미:
- 매 응답 전에 교정/선호/반복 패턴이 있는지 확인
- 필요 시 SESSION-STATE.md와 학습 로그에 즉시 반영
- 단순 답변이라도 개선 가능성을 점검하는 습관 유지

### Suggested Action
- 모든 프롬프트에서 미니 self-review를 수행
- 사용자 교정/선호/결정을 감지하면 WAL + learning log 우선
- 반복되는 패턴은 AGENTS.md/SOUL.md 승격 검토

### Metadata
- Source: user_feedback
- Related Files: /Users/chrischo/.openclaw/workspace-ellie/SESSION-STATE.md
- Tags: self-learning, preference, workflow, improvement

---
