# Student Consultant Service / App — Market Review Draft

작성일: 2026-03-11 08:22 PDT  
상태: draft v0.1  
범위: **마켓 검토 우선** (학생 관리, 선생 매치, 학생 프로젝트 관리가 가능한 학생 컨설턴트용 서비스/앱)

---

## 1. 한 줄 가설

**학생/학부모는 “좋은 선생 찾기”만 원하는 게 아니라, `매칭 + 진행관리 + 프로젝트/성과 추적`까지 한 번에 되는 구조를 원할 가능성이 높다.**  
현재 시장은 대체로 아래 3개로 쪼개져 있다.

1. **튜터 마켓플레이스**: Wyzant, Preply, Varsity Tutors  
2. **학생/학원 운영 SaaS**: TutorCruncher 계열, scheduling/billing/CRM 툴  
3. **프로젝트 기반 학습/멘토링 플랫폼**: Riipen, Crio 등  

즉, **학생 컨설턴트 비즈니스용 “운영 레이어”와 “결과 레이어”를 같이 잡는 통합 제품은 아직 포지셔닝 여지가 있음**.

---

## 2. 시장을 어떻게 볼지

이 사업은 사실 단일 시장이 아니다. 아래 3개 교집합에 가깝다.

### A. 온라인 튜터링/개인교습 시장
- Grand View Research 기준, **미국 온라인 private tutoring 시장은 2024년 약 $4.33B**, **2025~2030 CAGR 11.1%** 전망.
- 이는 이미 수요가 검증된 시장이라는 뜻.
- 특히 부모/학생은 **유연한 스케줄링**, **개인화 학습**, **시험/입시 지원**에 돈을 지불 중.

### B. 입시/교육 컨설팅 시장
- Marketplace/Town & Country 인용 기준, **독립 college counseling 업계는 약 $3B 규모**.
- 평균 가격대 언급도 존재: **약 $6,500**, 고가 지역은 **$15k 수준**, 극단적 high-end는 더 높음.
- 중요한 포인트: **입시/학업 컨설팅은 고가/고마진 서비스로 포지셔닝 가능**.

### C. 멘토링/프로젝트 기반 러닝 시장
- 멘토링 소프트웨어 시장은 리서치 기관별 편차가 크지만, **2025년 글로벌 수억~10억 달러대 추정**이 반복적으로 관측됨.
- Riipen 같은 플레이어는 **학생-실무 프로젝트 연결 + 프로젝트 관리 + 성과 추적**을 강조.
- 즉, 단순 과외보다 **career outcome / portfolio outcome** 중심 제품이 올라오고 있음.

### 해석
- **수요는 이미 존재**한다.
- 다만 수요가 **“과외”**, **“입시 컨설팅”**, **“프로젝트/커리어 멘토링”**으로 분산되어 있다.
- 따라서 기회는 새 시장 창조보다 **분산된 워크플로를 한 제품에 묶는 것**에 있음.

---

## 3. 고객 문제 정의

### 학생/학부모 입장 문제
1. **누가 좋은 선생인지 판단이 어렵다**  
   - 리뷰, 이력, specialty, 실제 결과가 분산됨.
2. **상담/수업/프로젝트 관리가 따로 논다**  
   - 카톡/문자 + 노션 + 구글캘린더 + 결제링크 + 문서 공유로 찢어짐.
3. **진행 상황이 안 보인다**  
   - 몇 회 수업 했는지, 목표 달성률, 과제, 포트폴리오 진행도 추적이 불편.
4. **입시/프로젝트 결과물이 남지 않는다**  
   - 수업은 했는데, 학생 profile/essay/project artifact/성과가 구조화되지 않음.

### 선생/컨설턴트 입장 문제
1. **리드 관리가 귀찮다**  
   - 문의, 상담, trial lesson, 견적, 결제 전환 흐름이 수기.
2. **운영툴이 조각남**  
   - 스케줄링/청구/메시징/자료공유/학생 관리가 통합 안 됨.
3. **차별화가 어렵다**  
   - 좋은 선생도 결국 profile listing 안에서 가격 경쟁으로 흐르기 쉬움.
4. **프로젝트형 서비스 운영이 특히 어렵다**  
   - 학생별 milestone, deliverable, 피드백 관리가 일반 tutoring SaaS와 잘 안 맞음.

---

## 4. 현재 시장 플레이어 맵

## 4.1 Tutor marketplace

### Wyzant
- 강점: 대형 tutor marketplace, 1:1 tutoring 흐름 성숙.
- 제품 특징: **실시간 영상, interactive whiteboard, 문서/텍스트/코드 편집** 제공.
- 시사점: **온라인 수업 experience 자체는 이미 commodity**.
- 약점/빈틈: 학생의 장기 목표 관리, 프로젝트 milestone, 컨설턴트 운영 CRM은 상대적으로 약함.

### Preply
- 강점: 대규모 tutor supply.
- 공개 사이트 기준: **100,000+ tutors**, **300,000+ 5-star reviews**.
- 시사점: 대규모 공급자 네트워크와 리뷰 기반 신뢰가 핵심.
- 약점/빈틈: language tutoring 중심 인식이 강하고, 고부가가치 student consulting workflow와는 거리 있음.

### Varsity Tutors
- 강점: tutoring + classes + test prep 확장형 브랜드.
- 시사점: marketplace가 단순 tutor listing을 넘어 **broader learning platform**으로 확장 가능함을 보여줌.
- 약점/빈틈: 학생별 프로젝트 운영/성과물 관리에 특화된 느낌은 약함.

## 4.2 Tutoring operations / back-office SaaS

### TutorCruncher 계열
- 핵심 가치: **scheduling, attendance, billing, tutor-client-student management** 통합.
- 시사점: 운영툴 수요는 분명함.
- 약점/빈틈: 보통 **학원/운영자 중심**으로 설계되어 있고, 학생 outcome이나 프로젝트 결과 관리가 약할 수 있음.

## 4.3 Project-based learning / mentorship

### Riipen
- 핵심 가치: **match, manage, report**.
- 공개 페이지 기준, 교육자는 **real company projects와 매칭**, 학생 onboarding, **project management**, outcome tracking 가능.
- 시사점: 네가 구상하는 "학생 프로젝트 관리"는 별도 category로도 설득력이 있음.
- 약점/빈틈: 학교/대학/기관형 느낌이 강하고, 개인 학생 컨설턴트 비즈니스에는 무거울 수 있음.

### Crio / 유사 프로젝트 러닝 플랫폼
- 강점: 실무형 프로젝트 + 멘토링.
- 시사점: 단순 수업보다 **portfolio/career outcome**을 팔 수 있다는 증거.
- 약점/빈틈: 특정 분야(예: 개발) 중심으로 수직화되는 경향.

---

## 5. 경쟁 구도에서 보이는 핵심 인사이트

### 인사이트 1 — “좋은 선생을 찾는 문제”는 이미 해결 경쟁이 심함
단순 매칭 marketplace만 하면 Wyzant/Preply/Varsity와 정면승부다.  
이 영역은 **공급자 규모, 리뷰 수, 브랜딩, CAC** 싸움이라 신생 서비스가 불리하다.

### 인사이트 2 — “운영 관리”만 하면 B2B SaaS 전쟁으로 들어감
TutorCruncher 류와 붙게 된다.  
이건 기능 체크리스트 게임이 되기 쉽다.

### 인사이트 3 — 기회는 “student outcome system”에 있음
즉, 너의 포지션은 그냥 tutoring app이 아니라:

> **학생 컨설턴트/멘토가 학생 1명을 장기적으로 관리하면서, 매칭·수업·프로젝트·성과물을 하나의 timeline으로 운영하는 system**

여기에 차별화 포인트가 있다.

### 인사이트 4 — 고가 서비스일수록 소프트웨어 니즈가 커진다
입시/포트폴리오/프로젝트 코칭은 객단가가 높다.  
객단가가 높을수록 선생/컨설턴트는 아래를 원한다.
- CRM
- 상담 기록
- 학생별 전략
- deliverables tracking
- parent updates
- billing automation

즉 **고가 컨설턴트용 vertical SaaS** 또는 **marketplace + ops hybrid**가 가능하다.

---

## 6. 초기 진입 방향 제안

완전한 양면 marketplace로 시작하는 건 무겁다.  
초기엔 아래 3개 중 하나가 현실적이다.

### Option A. Consultant Operating System (추천)
대상: 소규모 학생 컨설턴트/학업 코치/입시 멘토

핵심 기능:
- 학생 CRM
- 상담/수업 기록
- teacher/mentor assignment
- milestone/project tracking
- 자료/에세이/산출물 관리
- 결제/스케줄링/리마인더
- 학부모 업데이트 리포트

장점:
- 공급/수요 양면 확보 문제를 피함
- B2B-ish recurring revenue 가능
- 실제 painkiller 가능성 높음

리스크:
- 처음엔 marketplace처럼 겉으로 화려하지 않음
- 컨설턴트 workflow 이해를 깊게 해야 함

### Option B. Premium Matching + Management
대상: 고가 학생/학부모 시장

핵심 가치:
- vetted mentor/consultant matching
- 매칭 후 진행관리/성과 추적까지 제공

장점:
- 고객이 이해하기 쉬움
- take rate 모델 가능

리스크:
- 초기에 신뢰/공급 확보가 hardest problem

### Option C. Project-first Student Growth Platform
대상: 포트폴리오/캡스톤/연구/창업 프로젝트를 원하는 학생

핵심 가치:
- mentor matching
- project brief / milestone / review / artifact / showcase
- 결과물 중심 성장 스토리 축적

장점:
- 차별화 강함
- AI 시대에 “실제 결과물” 강조 포지션 좋음

리스크:
- 니치일 수 있음
- ICP를 잘못 잡으면 시장이 좁아질 수 있음

---

## 7. 지금 시점에서 가장 유력한 포지셔닝

현 시점 가설상 가장 설득력 있는 문장:

> **"학생 컨설턴트와 멘토를 위한 student success operating system"**

혹은

> **"매칭에서 끝나지 않고, 학생의 프로젝트와 결과물까지 관리하는 premium education workflow platform"**

이 포지션의 장점:
- Wyzant 같은 범용 tutor marketplace와 다름
- TutorCruncher 같은 범용 운영툴과도 다름
- Riipen 같은 기관형 프로젝트 플랫폼보다 더 개인 컨설턴트 친화적일 수 있음

---

## 8. MVP에서 꼭 검증해야 할 것

### 가설 1
컨설턴트들은 학생 관리/상담 기록/과제/프로젝트/학부모 리포트를 한곳에 모으는 pain이 크다.

### 가설 2
학생/학부모는 단순 tutor match보다 **진행 visibility**에 돈을 낼 수 있다.

### 가설 3
입시/포트폴리오/프로젝트 코칭은 일반 과외보다 higher willingness to pay가 있다.

### 가설 4
초기엔 marketplace보다 **관리툴**로 시작하고, 나중에 추천/매칭 레이어를 올리는 순서가 더 낫다.

---

## 9. MVP 기능 우선순위 초안

### P0
- 학생 프로필
- 목표/관심사/학년/지원전략 기록
- 선생/멘토 assignment
- 세션 노트
- 프로젝트 milestone 보드
- 파일/링크/산출물 첨부
- 캘린더/리마인더
- 상태 대시보드

### P1
- 학부모 요약 리포트 자동 생성
- 간단 결제/패키지 관리
- AI meeting note 정리
- 추천 mentor shortlist

### P2
- 양면 marketplace
- tutor discovery ranking
- 프로젝트 템플릿 마켓
- outcome analytics benchmark

---

## 10. 시장 검토 기준 임시 결론

### 결론
**시장 자체는 있다.**  
하지만 “또 하나의 과외앱”은 경쟁이 세다.  
반대로 **학생 컨설턴트/멘토가 학생 한 명의 여정을 관리하는 vertical workflow** 쪽은 아직 들어갈 틈이 있다.

### 가장 중요한 포인트
- **매칭만으론 약함**
- **운영툴만으론 평범함**
- **결과물/프로젝트/학생 성장 timeline**까지 묶어야 차별화 가능

### 초기 사업성 관점
- low-end mass market보다 **premium/high-trust niche**가 더 나아 보임
- 예: college counseling, STEM project mentorship, portfolio coaching, founder/creator youth mentoring

---

## 11. 다음 리서치 질문

다음 단계에서 검증할 것:
1. **ICP 정의**: 누구부터 잡을지?  
   - 입시 컨설턴트  
   - STEM 프로젝트 멘토  
   - 유학생/학부모 대상 컨설턴트  
   - 학원형 운영자  
2. **가격 모델**: SaaS subscription vs take rate vs hybrid  
3. **경쟁사 deep dive**: Wyzant / TutorCruncher / Riipen / niche admissions tools  
4. **사용자 인터뷰 가설**: 컨설턴트 10명, 학부모 10명, 학생 10명  
5. **MVP 화면 구조**: dashboard, student page, project board, mentor matching, parent report  

---

## 12. 참고 소스

### Market / industry
- Grand View Research — U.S. Online Private Tutoring Market  
  https://www.grandviewresearch.com/industry-analysis/us-online-private-tutoring-market-report
- Marketplace — Inside the $3 billion independent college counseling industry  
  https://www.marketplace.org/story/2025/03/24/inside-the-3-billion-independent-college-counseling-industry

### Competitor / product references
- Wyzant Learning Studio  
  https://support.wyzant.com/online-learning-studio/online-learning-studio-faqs/how-does-online-tutoring-work/
- Preply  
  https://preply.com/
- Varsity Tutors  
  https://www.varsitytutors.com/
- TutorCruncher — Tutoring Software Explained  
  https://tutorcruncher.com/blog/tutoring-software-explained
- Riipen for Educators  
  https://www.riipen.com/product/educators

---

## 13. 내 현재 추천

**Step 1은 marketplace가 아니라 "컨설턴트 운영 + 학생 결과 관리" SaaS 관점으로 검토를 이어가는 게 맞다.**  
그 다음에:
- 추천/매칭 레이어 추가
- 학부모 리포트 자동화
- 프로젝트/포트폴리오 evidence system 강화

이 순서가 더 현실적이다.
