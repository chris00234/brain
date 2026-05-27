# Counselor Platform — Demo Guide with Screenshots

---

## 접속 정보

- **URL**: `http://localhost:3099` (로컬) 또는 Vercel 배포 URL
- **인증**: 없음 (데모 모드)
- **역할 전환**: 로그인 화면에서 선택

---

## Login — 역할 선택 화면

![[counselor-demo-screenshots/01-login.jpg]]

| 항목 | 설명 |
|------|------|
| **좌측 히어로** | 브랜딩 + "Less Admin, More Counseling" + 5개 핵심 가치 |
| **우측 카드** | Counselor / Teacher / Parent 3개 역할 |
| **동작** | 카드 클릭 → 해당 역할 뷰로 즉시 진입 |
| **화살표** | 각 카드 우측에 `→` 표시 (클릭 어포던스) |

---

# 🔵 Counselor (카운슬러)

카운슬러는 4개 메뉴를 사용합니다: **Dashboard, Students, Calendar, Billing**

---

## 1. Dashboard

![[counselor-demo-screenshots/02-dashboard.jpg]]

### 화면 구성
| 영역 | 내용 |
|------|------|
| **메트릭 카드 4개** | 🔵 Total Students (10) · 🔴 At Risk (2) · 🟢 Essays In Progress (3) · 🟣 Consultations (3) |
| **Priority Students 테이블** | 위험도 순 정렬 (At Risk → On Track → Ahead), 각 학생별 상태 뱃지 + 파이프라인 진행률 |
| **Upcoming Timeline** | 다가오는 이벤트 6개 (상담, 수업, 마감일) 시간순 |
| **View all →** | 클릭 시 전체 학생 목록으로 이동 |

### 동작
- 메트릭 카드: 색상으로 즉시 구분 (빨간색 = 위험 지표)
- 학생 이름 클릭 → 학생 상세 페이지로 이동
- 각 카드에 트렌드 화살표 (↑/↓) 표시

### 데모 포인트
> "아침에 이 화면 한 번 보면 30초 만에 오늘 뭘 해야 하는지 다 보입니다."

---

## 2. Students — 학생 관리

![[counselor-demo-screenshots/03-students-list.jpg]]

### 화면 구성
| 영역 | 내용 |
|------|------|
| **메트릭 카드 4개** | 🔵 Total Students · 🔴 At Risk · 🟢 Avg GPA · 🟣 Applications |
| **검색창** | 학생 이름/ID 실시간 검색 (타이핑 즉시 필터링) |
| **상태 필터** | All / On Track / At Risk / Ahead 버튼 (즉시 반응) |
| **학생 테이블** | 이름, 학년, GPA, 상태 뱃지, 단계, 파이프라인 %, 담당 선생님, 지원 대학 수 |
| **Clear 버튼** | 필터 활성 시 나타남 → 초기화 |

### 동작
- 검색: 타이핑하면 즉시 필터링 (Enter 불필요)
- 필터: "At Risk" 클릭 → 테이블에 위험 학생만 표시, "Showing 2 of 10" 업데이트
- "Open" 버튼 또는 이름 클릭 → 학생 상세 이동

### 데모 포인트
> "검색과 필터가 즉시 반응합니다. 30명, 50명이 되어도 한눈에 찾을 수 있어요."

---

## 3. Student Detail — 학생 상세

![[counselor-demo-screenshots/04-student-detail.jpg]]

### 화면 구성
| 영역 | 내용 |
|------|------|
| **프로필 헤더** | 이름, 학년, GPA, SAT/ACT, 상태 뱃지 (On Track / At Risk / Ahead) |
| **파이프라인 바** | 6단계 원형 스텝: Research → School List → Essays → Application → Submitted → Decision |
| **담당 선생님** | 배정된 선생님 이름 표시 |
| **Overview 탭** | 대학 리스트 (🔴 Reach / 🟡 Match / 🟢 Safety 분류), 각 대학별 서류 체크리스트 (✅/❌) |
| **Timeline 탭** | 학생 관련 모든 이벤트 시간순 |
| **Documents 탭** | 대학별 서류 완성도 프로그레스 바 |
| **Notes 탭** | 상담 메모 — 요약, 결정 사항, 할 일 목록 |

### 파이프라인 바 동작
- ✅ 완료 단계: 파란 원 + 체크마크
- 🔵 현재 단계: 파란 원 + glow 효과 + 파란 라벨
- ⚪ 미래 단계: 회색 원 + 숫자
- 단계 간 연결선 색상이 진행도에 따라 변함

### 데모 포인트
> "학부모가 '우리 아이 어떻게 되고 있어요?' 하면, 이 화면 하나 열면 됩니다. 기억에 의존하지 않아요."

---

## 4. Calendar — 일정 관리

![[counselor-demo-screenshots/05-calendar.jpg]]

### 화면 구성
| 영역 | 내용 |
|------|------|
| **요약 카드 5개** | Total Events, Consultations, Classes, Deadlines, Exams |
| **필터** | 학생별 / 선생님별 드롭다운 + "Clear filters" |
| **FullCalendar** | 월간/주간 뷰 전환, today 버튼 |
| **Day Sidebar** | 선택한 날짜의 이벤트 리스트 + 레전드 |

### 색상 코드
| 색상 | 이벤트 타입 |
|------|------------|
| 🔵 파란색 | 상담 (Consultation) |
| 🟢 초록색 | 수업 (Class) |
| 🔴 빨간색 | 마감일 (Deadline) |
| 🟡 주황색 | 시험 (Exam) |

### 동작
- 날짜 클릭 → Day Sidebar에 그날 이벤트 표시
- 이벤트 클릭 → 상세 Dialog 팝업 (시간, 학생, 선생님, 타입)
- Dialog에서 "Open student profile" 클릭 → 학생 상세 이동
- 필터로 특정 선생님/학생 일정만 보기

### 데모 포인트
> "모든 일정이 여기 모입니다. 카톡으로 '언제 되세요?' 주고받는 시간이 사라집니다."

---

## 5. Billing — 수입/지출 관리

![[counselor-demo-screenshots/06-billing.jpg]]

### 화면 구성
| 영역 | 내용 |
|------|------|
| **탭** | "Parent Invoices" / "Teacher Payouts" 전환 |
| **메트릭 카드 4개** | 🔵 Total Revenue ($108K) · 🟣 Total Payouts ($7.9K) · 🟡 Outstanding ($37K) · 🟢 Net Profit ($63K) |
| **Parent Invoice 리스트** | 학부모별 계약 (이름, 이메일, 학생, 기간, 금액, 납부, 잔액) |
| **상태 뱃지** | 🟢 Paid · 🟡 Partial · 🔴 Overdue · ⚪ Pending |
| **Invoice Detail** | 우측 패널 — 계약 클릭 시 결제 이력 상세 |
| **Teacher Payouts** | 선생님별 월간 시수, 시급, 금액, 상태 |
| **Mark as Paid** | Pending 상태에서 클릭 → Paid로 변경 |

### 동작
- 계약 카드 클릭 → 우측에 결제 이력 표시 (날짜, 금액, 방법)
- "Teacher Payouts" 탭 → 선생님별 정산 내역
- "Mark as Paid" 버튼 → 즉시 상태 변경

### 데모 포인트
> "수입과 지출이 한눈에 보입니다. '미수금이 $37K이네요.' 여기서 바로 관리합니다."

---

# 🟢 Teacher (선생님)

선생님은 3개 메뉴를 사용합니다: **My Students, My Schedule, My Earnings**

---

## Teacher Dashboard

![[counselor-demo-screenshots/07-teacher-dashboard.jpg]]

### 화면 구성
| 영역 | 내용 |
|------|------|
| **선생님 배너** | 이름(Ms. Jennifer Lee), 과목(Essay Writing), 이니셜 아바타, 선생님 전환 드롭다운 |
| **통계 카드 3개** | 🔵 My Students (7) · 🟢 Upcoming Classes (5) · 🟣 Total Hours (46h) |
| **수입 카드 3개** | Total Earned ($2,760) · Pending Payout ($1,440) · Total Hours (46h) |
| **My Students** | 담당 학생 리스트 + 상태 뱃지 + "Report" 버튼 |
| **Class Schedule** | 다가오는 수업 일정 (날짜, 시간, 학생) |
| **Earnings Summary** | 월별 정산 이력 (시수 × 시급 = 금액, Paid/Pending) |

### 동작
- 선생님 드롭다운 → 다른 선생님으로 전환 시 모든 데이터 업데이트
- "Report" 버튼 클릭 → 수업 리포트 작성 Dialog
  - 텍스트 입력: "Student progress, homework, and next steps..."
  - "Save notes" 또는 "Cancel"
- 선생님별 정산: Pending/Paid 상태 확인

### 데모 포인트
> "선생님은 수업 끝나고 리포트 하나만 쓰면 됩니다. 5분이면 끝나요. 이게 카운슬러 대시보드에 바로 반영됩니다."

---

# 🟡 Parent (학부모)

학부모는 2개 메뉴를 사용합니다: **My Child, Billing**

---

## Parent Portal

![[counselor-demo-screenshots/08-parent-portal.jpg]]

### 화면 구성
| 영역 | 내용 |
|------|------|
| **페이지 타이틀** | "[자녀 이름]'s Progress" (예: Sarah Kim's Progress) |
| **자녀 선택** | 여러 자녀 카드 (클릭으로 전환) |
| **Application Status** | 현재 단계 + 프로그레스 바 (50% complete) + 상태 뱃지 |
| **Billing at a Glance** | 총액, 납부액, 잔액, 상태 뱃지, 최근 결제 이력 |
| **Upcoming Deadlines** | 마감일 리스트 (UC Berkeley Essay, Stanford Recommendation 등) + "Due" 뱃지 |
| **Upcoming Sessions** | 예정된 상담/수업 일정 (consultation, class, exam 타입) |
| **Documents Checklist** | 대학별 서류 완성도 프로그레스 바 (Stanford EA 3/4, UC Berkeley RD 2/4 등) |
| **Recent Consultations** | 최근 상담 요약 + "Next steps" 할 일 목록 |
| **Need Help?** | "Open billing" 버튼 |

### 동작
- 자녀 카드 클릭 → 선택된 자녀로 전체 데이터 전환
- 프로그레스 바로 한눈에 진행 상황 파악
- 서류 체크리스트: 대학별 몇 개 완료했는지 시각적으로 표시
- 상담 요약: 결정 사항과 다음 할 일까지 공유

### 데모 포인트
> "학부모님이 불안할 때 이 화면 한 번 열면 안심이 됩니다. '체계적으로 관리되고 있구나.' 확인 전화 안 해도 됩니다."

---

## 공통 기능

### 🔔 알림 벨 (모든 역할)
- 헤더 우상단 벨 아이콘 + 빨간 뱃지 (읽지 않은 수)
- 클릭 → 알림 드롭다운
- 읽지 않은 알림: 🔵 파란 dot + 진한 텍스트 + 연한 파란 배경
- 읽은 알림: 흰 배경 + 회색 텍스트
- 알림 클릭 → 해당 페이지로 이동

### 🔍 검색 (Counselor)
- 헤더 검색창에 학생 이름 입력 + Enter → Students 검색 결과

### 👤 역할 표시
- 헤더 우상단: 현재 역할 뱃지 (Counselor / Teacher / Parent)
- 아바타: 역할별 이니셜 (CS / TC / PR)

---

## 데모 시나리오 (12분)

| 시간 | 화면 | 말할 것 | 핵심 |
|------|------|---------|------|
| 0-1분 | Login | "3가지 역할이 있습니다. 먼저 카운슬러로 들어가겠습니다." | 실제 제품감 |
| 1-3분 | Dashboard | "아침에 이 화면 한 번 보면 됩니다. 위험 학생 2명, 이번 주 상담 3건." | 한눈에 파악 |
| 3-5분 | Student Detail | "Daniel Park — 현재 School List 단계, 추천서 미완료. 바로 보입니다." | 체계적 관리 |
| 5-7분 | Calendar | "모든 일정이 색상으로 구분됩니다. 선생님별로 필터도 됩니다." | 카톡 대체 |
| 7-9분 | Billing | "수입 $108K, 지출 $7.9K, 미수금 $37K. 한눈에 봅니다." | 비즈니스 관리 |
| 9-10분 | Login → Parent | "이제 학부모 입장에서 보겠습니다." | 역할 전환 |
| 10-11분 | Parent Portal | "자녀 진행 상황, 마감일, 서류, 빌링 — 전부 여기 있습니다." | 학부모 안심 |
| 11-12분 | 마무리 | "AI 에세이 분석, 대학 추천은 다음 단계에 추가됩니다." | 확장성 |

---

## Mock Data 참고

| 데이터 | 수량 |
|--------|------|
| 학생 | 10명 (Sarah Kim, Daniel Park, Emily Choi, Jason Lee, Olivia Wang, Ryan Chen, Grace Yoon, Alex Hwang, Sophie Lim, Ethan Cho) |
| 선생님 | 4명 (Ms. Lee/Essay, Mr. Park/Math, Ms. Johnson/SAT, Mr. Kim/Science) |
| 대학 | 16개 (Stanford, Harvard, MIT, UC Berkeley, Cornell 등) |
| 캘린더 | 28개 이벤트 (2주치) |
| 계약 | 8건 ($10K~$18K) |
| 정산 | 8건 (2-3월) |
| 알림 | 8개 (4개 미읽음) |
