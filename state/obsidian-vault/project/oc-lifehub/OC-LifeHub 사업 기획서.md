---
type: business-spec
project: oc-lifehub
status: planning
created: '2026-02-13'
updated: '2026-02-13'
tags:
  - project
  - oc-lifehub
  - business-plan
target-competitor: radiokorea.com
target-age: 20-50s
---

# OC-LifeHub 사업 기획서

## 1. 프로젝트 개요

| 항목 | 내용 |
|------|------|
| **프로젝트명** | OC-LifeHub |
| **비전** | RadioKorea를 대체하는 남가주 최대 한인 커뮤니티 플랫폼 |
| **타겟 지역** | Orange County, California (→ 남가주 전체 확장) |
| **컨셉** | 20-50대를 위한 현대적 한인 생활 포털 |
| **핵심 가치** | 뉴스 + 커뮤니티 + 생활서비스를 하나의 모던 플랫폼에서 |
| **타겟 연령** | 20-50대 (디지털 네이티브 + 경제활동 핵심 인구) |
| **목표 사용자** | OC/LA 거주 한인 + 현지인 (이중 언어 지원) |
| **벤치마크** | RadioKorea (~593K 월간 방문) → 1년 내 추월 목표 |

---

## 2. 왜 RadioKorea를 이길 수 있는가?

### RadioKorea 현황 분석
- **월간 트래픽**: ~593K 방문 (SimilarWeb 기준)
- **주요 기능**: 라디오 스트리밍, 뉴스, 벼룩시장, 구인구직, 부동산, 업소록, 자동차, 법률/재정 상담
- **수익원**: 광고, 업소 등록, 라디오 방송 광고

### RadioKorea의 치명적 약점
| 약점 | 상세 | OC-LifeHub 기회 |
|------|------|-----------------|
| **올드한 UI/UX** | 2000년대 스타일 디자인, 복잡한 내비게이션 | 모던 UI + 모바일 퍼스트 |
| **느린 로딩 속도** | 서버 사이드 렌더링 미흡, 과도한 광고 | Next.js SSR/SSG + CDN |
| **모바일 경험 부족** | 반응형 미흡, 앱 품질 낮음 | PWA + 네이티브급 모바일 |
| **20-30대 이탈** | 젊은 세대가 사용하기 불편 | SNS 연동, 모던 인터랙션 |
| **OC 특화 부족** | LA 중심, OC 콘텐츠 부족 | OC 하이퍼로컬 특화 |
| **검색/필터 부실** | 정보 찾기 어려움 | AI 기반 검색 + 스마트 필터 |
| **개인화 없음** | 모든 사용자에게 동일 콘텐츠 | AI 추천 + 관심사 기반 피드 |
| **커뮤니티 활성도 낮음** | 게시판 형식 구닥다리 | 실시간 채팅 + 리액션 + 투표 |

### 핵심 전략: "RadioKorea의 모든 기능 + 현대적 UX + 바이럴 콘텐츠"

---

## 3. 타겟 연령별 공략 전략 (20-50대)

### 20대: "발견과 연결"
| 니즈 | 기능 | 후킹 포인트 |
|------|------|------------|
| 이벤트/파티 | 이벤트 캘린더 + 티켓팅 | "이번 주말 OC 어디가?" |
| 맛집 탐색 | 맛집 지도 + 인스타 연동 | 사진 중심 리뷰, 릴스 스타일 |
| 중고거래 | 당근마켓 스타일 직거래 | 위치 기반 + 채팅 거래 |
| 구직 | 알바/인턴 정보 | 즉시 지원 + 채팅 면접 |
| 소셜 | 관심사 기반 모임 | 소모임 + 오프라인 밋업 |

### 30대: "정착과 성장"
| 니즈 | 기능 | 후킹 포인트 |
|------|------|------------|
| 부동산 | 매물 검색 + 학군 정보 | "어바인 학군별 집값 트렌드" |
| 육아 | 육아 커뮤니티 + 학원 정보 | 엄마/아빠 모임 |
| 커리어 | 풀타임 구인구직 | 이력서 빌더 + 기업 리뷰 |
| 비즈니스 | 업소록 + 리뷰 | 신뢰 기반 업소 추천 |
| 법률/이민 | 전문가 Q&A | 무료 상담 매칭 |

### 40-50대: "커뮤니티와 영향력"
| 니즈 | 기능 | 후킹 포인트 |
|------|------|------------|
| 뉴스 | 한인 뉴스 + 분석 | "오늘의 한인 뉴스 5분 요약" |
| 투자 | 부동산/주식/비즈니스 정보 | 투자 커뮤니티 |
| 비즈니스 | 사업주 네트워킹 | B2B 연결 + 광고 플랫폼 |
| 건강 | 한인 병원/약국 정보 | 건강 칼럼 + 전문의 Q&A |
| 골프/여가 | 동호회 + 모임 | OC 골프장 예약/리뷰 |

---

## 4. 초기 트래픽 폭발 전략 (Phase 1)

> **핵심 원칙:** RadioKorea를 이기려면 초기 6개월 내 월 10만 방문자를 확보해야 한다. 콘텐츠 양 + 바이럴 + SEO 삼각 편대로 공략한다.

### 4.1 콘텐츠 크롤링 + AI 가공 파이프라인

#### 크롤링 대상 소스 (RadioKorea 대비 2배 이상)
| 카테고리 | 소스 | 가공 방식 |
|----------|------|-----------|
| **OC 뉴스** | OC Register, Voice of OC, Patch | AI 요약 + 한국어 번역 |
| **한인 뉴스** | 중앙일보, 한국일보, 연합뉴스 | 요약 + 출처 링크 |
| **이벤트** | Eventbrite, Meetup, 시 공식사이트 | 자동 수집 + 캘린더 등록 |
| **맛집** | Yelp, Google Maps | 한인 맛집 큐레이션 |
| **부동산** | Zillow, Redfin API | 한글 필터 + 학군 매핑 |
| **구인구직** | Indeed, LinkedIn, 한인 구인 | 카테고리 분류 + 알림 |
| **중고거래** | Craigslist, Facebook Marketplace | 한인 타겟 필터링 |
| **날씨/교통** | Weather.gov, OCTA, Google Traffic | 실시간 위젯 |

#### AI 콘텐츠 가공 (차별화 핵심)
- **AI 뉴스 요약**: 원문 기사 → 3줄 요약 + 핵심 키워드 추출
- **자동 번역**: 영어 뉴스 → 한국어 (DeepL API + GPT 후처리)
- **감성 분석**: 뉴스 톤 분석 → 긍정/부정/중립 태그
- **자동 카테고리**: ML 기반 뉴스 자동 분류
- **트렌드 감지**: 급상승 키워드 자동 감지 → 속보 알림

### 4.2 바이럴 콘텐츠 전략 (트래픽 확보 핵심)

#### "OC 한인 필수 콘텐츠" 시리즈
| 콘텐츠 유형 | 예시 | 바이럴 요소 |
|-------------|------|------------|
| **🔥 OC 핫플 TOP 10** | "어바인 데이트 코스 TOP 10" | 리스트형, 공유 유도 |
| **💰 OC 생활 꿀팁** | "OC에서 월세 $500 아끼는 방법" | 실용 정보, 저장 유도 |
| **🍜 OC 맛집 지도** | "OC 한식당 완전 정복 지도" | 인터랙티브 지도, 참여 유도 |
| **📊 OC 데이터** | "OC 도시별 한인 인구 통계" | 인포그래픽, 공유 가치 |
| **🗳️ OC 투표/설문** | "OC 최고 한식당은?" | 참여형, 댓글 유도 |
| **📸 OC 포토 챌린지** | "#MyOCLife 사진 공모전" | UGC, 인스타 연동 |

#### 숏폼 비디오 전략
- **YouTube Shorts / Instagram Reels / TikTok** 동시 운영
- "OC 1분 뉴스" — 매일 아침 한인 뉴스 1분 요약
- "OC 맛집 30초" — 숏폼 맛집 리뷰
- "OC 리얼 라이프" — 한인 일상 브이로그

### 4.3 SEO 핵폭탄 전략

#### 키워드 매트릭스 (도시 × 카테고리 × 언어)
```
[OC 34개 도시] × [20개 카테고리] × [한/영] = 1,360개 랜딩 페이지
```

| 키워드 유형 | 예시 | 월간 검색량 (추정) |
|-------------|------|-------------------|
| 도시+카테고리 | "irvine korean restaurant" | 1,000+ |
| 한글 도시명 | "어바인 한국 식당" | 500+ |
| 이벤트 | "OC events this weekend" | 2,000+ |
| 생활정보 | "orange county korean community" | 800+ |
| 부동산 | "irvine homes for sale korean" | 300+ |

#### 프로그래매틱 SEO
- 도시별 자동 생성 페이지: `/irvine`, `/fullerton`, `/buena-park` 등
- 카테고리별 자동 생성: `/restaurants`, `/events`, `/real-estate`
- **각 페이지 SSG (Static Site Generation)** → Google 즉시 인덱싱
- 구조화 데이터 100% 적용 (Schema.org: Event, Article, LocalBusiness, Restaurant)
- 사이트맵 자동 생성 + Google/Bing/Naver 검색 등록

### 4.4 소셜 미디어 공격적 운영
| 채널 | 전략 | 게시 빈도 |
|------|------|-----------|
| **Instagram** | OC 맛집/이벤트 사진 + Reels | 매일 1-2포스트 |
| **TikTok** | OC 숏폼 콘텐츠 | 매일 1-2개 |
| **YouTube** | OC 뉴스 요약, 맛집 리뷰 | 주 2-3회 |
| **Facebook** | OC 한인 그룹 타겟 공유 | 매일 |
| **KakaoTalk** | 오픈채팅방 "OC 한인 라이프" 운영 | 상시 |
| **Thread/X** | 뉴스 속보 + 커뮤니티 토론 | 매일 |

### 4.5 한인 커뮤니티 오프라인 침투
- OC 한인 교회 게시판에 전단지 배포
- 한인 마트 (H Mart, Zion Market) 내 QR 코드 스티커
- 한인 축제/행사 스폰서 참여 (Korean Festival, Lunar New Year 등)
- 한인 업소 무료 등록 캠페인 ("업소록에 무료로 등록하세요!")
- 한인 단체/협회 협업 (한인회, 한인상공회의소)

### 4.6 레퍼럴 & 게이미피케이션
- **초대 보상 시스템**: 친구 초대 시 포인트 적립
- **포인트 시스템**: 글 작성, 댓글, 리뷰 → 포인트 적립 → 한인 업소 쿠폰 교환
- **랭킹 시스템**: 월간 활동 랭킹 → 배지 + 특전
- **출석 체크**: 매일 접속 보상 → 습관 형성

---

## 5. 웹사이트 기능 로드맵

### Phase 1: 트래픽 머신 (MVP) — 0~3개월
> RadioKorea보다 더 빠르게, 더 많이, 더 깔끔하게 정보를 보여준다.

| 기능             | 설명                  | RadioKorea 대비   |
| -------------- | ------------------- | --------------- |
| 🗞️ **뉴스 허브**  | AI 요약 + 이중 언어 뉴스 피드 | 더 빠른 속도 + AI 요약 |
| 📅 **이벤트 캘린더** | OC 전체 이벤트 통합 캘린더    | 인터랙티브 지도 + 필터   |
| 🍜 **맛집 지도**   | 인터랙티브 맵 + 사진 리뷰     | 비주얼 중심 + 필터     |
| 🔍 **통합 검색**   | AI 기반 스마트 검색        | 자동완성 + 관련 추천    |
| 🌤️ **생활 위젯**  | 날씨, 교통, 환율, 주요 뉴스   | 대시보드형 한 눈에      |
| 🌐 **한영 전환**   | 원클릭 언어 전환           | 자동 번역 품질 차별화    |
| 📱 **모바일 퍼스트** | PWA + 푸시 알림         | 앱 수준 모바일 경험     |

### Phase 2: 커뮤니티 + 생활서비스 — 3~6개월
> RadioKorea의 핵심 기능을 모두 흡수 + 현대화

| 기능 | 설명 | RadioKorea 대비 |
|------|------|----------------|
| 👤 **회원 시스템** | OAuth (Google, Kakao, Apple) | 원클릭 가입 |
| 💬 **커뮤니티 게시판** | 자유/질문/정보/동호회 | 실시간 + 리액션 + 투표 |
| 🛒 **벼룩시장** | 중고거래 (당근마켓 스타일) | 위치 기반 + 채팅 + 안전결제 |
| 💼 **구인구직** | 채용 정보 + 이력서 | 즉시지원 + AI 매칭 |
| 🏠 **부동산** | 매물 + 학군 + 에이전트 | 지도 기반 + 학군 오버레이 |
| 🚗 **자동차** | 중고차/딜러 정보 | Carfax 연동 + 가격 비교 |
| 🏪 **업소록** | 한인 비즈니스 디렉토리 | 지도 + 리뷰 + 쿠폰 |
| ⚖️ **전문가 Q&A** | 법률/이민/세무/보험 상담 | AI 사전 답변 + 전문가 매칭 |

### Phase 3: 수익화 + 플랫폼 확장 — 6~12개월
> 트래픽 기반 수익화 + RadioKorea 트래픽 추월

| 기능 | 설명 | 목표 |
|------|------|------|
| 📣 **광고 플랫폼** | 셀프서비스 광고 (배너, 스폰서) | 한인 업소 직접 광고 집행 |
| 🎫 **이벤트 티켓팅** | 자체 티켓 판매 시스템 | 수수료 수익 |
| 💎 **프리미엄 업소** | 업소 프로필 강화 (사진, 쿠폰, 상위노출) | 월 구독 수익 |
| 📱 **네이티브 앱** | React Native 앱 (iOS/Android) | 푸시 알림 + 리텐션 |
| 🤖 **AI 어시스턴트** | "OC 뭐 먹지?" 챗봇 | 개인화 추천 |
| 📊 **비즈니스 대시보드** | 업소 분석 (방문자, 리뷰 통계) | SaaS 수익 |
| 🌏 **지역 확장** | LA, San Diego로 확장 | LA-LifeHub, SD-LifeHub |

---

## 6. 기술 스택

### 프론트엔드
| 기술 | 이유 |
|------|------|
| **Next.js 15** (App Router) | SSR/SSG → SEO 최적화 + 프로그래매틱 SEO |
| **TypeScript** | 타입 안전성 |
| **Tailwind CSS** | 빠른 UI 개발 |
| **shadcn/ui** | 고품질 UI 컴포넌트 |
| **Framer Motion** | 부드러운 애니메이션 (모던 UX) |

### 백엔드
| 기술 | 이유 |
|------|------|
| **Next.js API Routes** | 프론트엔드와 통합, 서버리스 |
| **Supabase** | PostgreSQL + Auth + Storage + Realtime |
| **Prisma** | ORM, 타입 안전 쿼리 |
| **Redis (Upstash)** | 캐싱 + 실시간 기능 + Rate Limiting |

### AI/ML 파이프라인
| 기술 | 이유 |
|------|------|
| **Claude API** | 뉴스 요약, 콘텐츠 분류, 챗봇 |
| **DeepL API** | 고품질 한영 번역 |
| **Embedding + pgvector** | 시맨틱 검색 + 추천 시스템 |

### 크롤링 파이프라인
| 기술 | 이유 |
|------|------|
| **Python** + **Scrapy** | 대규모 크롤링 프레임워크 |
| **Playwright** | JS 렌더링 사이트 대응 |
| **Celery + Redis** | 비동기 작업 스케줄링 |
| **APScheduler** | 주기적 수집 스케줄 |

### 인프라
| 기술 | 이유 |
|------|------|
| **Vercel** | Next.js 최적 배포 + Edge Functions |
| **Supabase Cloud** | DB + Auth + Storage |
| **Railway** | 크롤링/AI 파이프라인 서버 |
| **Cloudflare** | CDN + DDoS + WAF |
| **Sentry** | 에러 모니터링 |

---

## 7. 데이터베이스 설계 (확장)

```sql
-- 뉴스 테이블
CREATE TABLE news (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  title TEXT NOT NULL,
  title_ko TEXT,
  summary TEXT NOT NULL,
  summary_ko TEXT,
  ai_summary TEXT,              -- AI 3줄 요약
  source_url TEXT NOT NULL UNIQUE,
  source_name TEXT NOT NULL,
  category TEXT NOT NULL,
  sentiment TEXT,                -- positive/negative/neutral
  keywords TEXT[],               -- AI 추출 키워드
  image_url TEXT,
  view_count INTEGER DEFAULT 0,
  share_count INTEGER DEFAULT 0,
  published_at TIMESTAMPTZ,
  crawled_at TIMESTAMPTZ DEFAULT NOW(),
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 이벤트 테이블
CREATE TABLE events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  title TEXT NOT NULL,
  title_ko TEXT,
  description TEXT,
  description_ko TEXT,
  location TEXT,
  address TEXT,
  city TEXT NOT NULL,
  latitude DECIMAL(10,7),
  longitude DECIMAL(10,7),
  start_date TIMESTAMPTZ NOT NULL,
  end_date TIMESTAMPTZ,
  source_url TEXT,
  source_name TEXT,
  category TEXT,
  image_url TEXT,
  price TEXT,
  is_free BOOLEAN DEFAULT FALSE,
  view_count INTEGER DEFAULT 0,
  bookmark_count INTEGER DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 비즈니스 디렉토리 (업소록)
CREATE TABLE businesses (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id UUID REFERENCES profiles(id),
  name TEXT NOT NULL,
  name_ko TEXT,
  category TEXT NOT NULL,
  subcategory TEXT,
  description TEXT,
  description_ko TEXT,
  address TEXT NOT NULL,
  city TEXT NOT NULL,
  phone TEXT,
  website TEXT,
  instagram TEXT,
  kakao_id TEXT,
  latitude DECIMAL(10,7),
  longitude DECIMAL(10,7),
  rating DECIMAL(2,1),
  review_count INTEGER DEFAULT 0,
  is_verified BOOLEAN DEFAULT FALSE,
  is_premium BOOLEAN DEFAULT FALSE,
  tier TEXT DEFAULT 'free',      -- free/basic/premium
  operating_hours JSONB,
  images TEXT[],
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 사용자 프로필
CREATE TABLE profiles (
  id UUID PRIMARY KEY REFERENCES auth.users(id),
  username TEXT UNIQUE NOT NULL,
  display_name TEXT,
  avatar_url TEXT,
  preferred_language TEXT DEFAULT 'ko',
  city TEXT,
  age_group TEXT,                -- '20s', '30s', '40s', '50s'
  interests TEXT[],
  points INTEGER DEFAULT 0,
  level INTEGER DEFAULT 1,
  referral_code TEXT UNIQUE,
  referred_by UUID REFERENCES profiles(id),
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 커뮤니티 게시판
CREATE TABLE posts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  author_id UUID REFERENCES profiles(id),
  board_type TEXT NOT NULL,      -- free/qna/market/jobs/housing/auto/clubs
  title TEXT NOT NULL,
  content TEXT NOT NULL,
  images TEXT[],
  price DECIMAL(10,2),           -- 벼룩시장/부동산/자동차용
  location TEXT,
  city TEXT,
  view_count INTEGER DEFAULT 0,
  like_count INTEGER DEFAULT 0,
  comment_count INTEGER DEFAULT 0,
  share_count INTEGER DEFAULT 0,
  is_pinned BOOLEAN DEFAULT FALSE,
  is_sold BOOLEAN DEFAULT FALSE, -- 거래 완료 여부
  tags TEXT[],
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 댓글
CREATE TABLE comments (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  post_id UUID REFERENCES posts(id) ON DELETE CASCADE,
  author_id UUID REFERENCES profiles(id),
  parent_id UUID REFERENCES comments(id),  -- 대댓글
  content TEXT NOT NULL,
  like_count INTEGER DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 리뷰
CREATE TABLE reviews (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id UUID REFERENCES businesses(id) ON DELETE CASCADE,
  author_id UUID REFERENCES profiles(id),
  rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
  content TEXT NOT NULL,
  images TEXT[],
  helpful_count INTEGER DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 포인트 이력
CREATE TABLE point_transactions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES profiles(id),
  amount INTEGER NOT NULL,
  type TEXT NOT NULL,            -- post/comment/review/referral/daily_check
  description TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 채팅 (벼룩시장 거래용)
CREATE TABLE chat_rooms (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  post_id UUID REFERENCES posts(id),
  buyer_id UUID REFERENCES profiles(id),
  seller_id UUID REFERENCES profiles(id),
  last_message TEXT,
  last_message_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
```

---

## 8. 수익 모델

| 수익원 | 설명 | 예상 월 수익 | 시작 시점 |
|--------|------|-------------|-----------|
| **Google AdSense** | 프로그래매틱 광고 | $200-500 | Phase 2 (월 5만+ 방문) |
| **로컬 배너 광고** | 한인 업소 배너/스폰서 | $500-2,000 | Phase 2 |
| **프리미엄 업소록** | 상위노출 + 쿠폰 + 분석 | $300-1,000 | Phase 2 |
| **구인구직 유료 게시** | 기업 채용 공고 ($50-200/건) | $500-1,500 | Phase 3 |
| **이벤트 프로모션** | 이벤트 홍보 유료화 | $200-500 | Phase 3 |
| **부동산 에이전트 광고** | 리얼터 프로필 광고 | $300-800 | Phase 3 |
| **자동차 딜러 광고** | 딜러 스폰서 | $200-500 | Phase 3 |
| **제휴 마케팅** | 로컬 딜/쿠폰 수수료 | $100-300 | Phase 3 |
| **합계 (12개월 목표)** | | **$2,300-7,100/월** | |

---

## 9. 경쟁 분석 (확장)

| 경쟁자 | 월간 트래픽 | 강점 | 약점 | 공략 전략 |
|--------|------------|------|------|-----------|
| **라디오코리아** | ~593K | 라디오 + 뉴스 + 벼룩시장, 높은 인지도 | 올드 UX, OC 비특화, 20-30대 이탈 | 모든 기능 흡수 + 모던 UX |
| **미주 중앙일보** | ~3.76M | 최대 트래픽, 뉴스 전문 | 커뮤니티 부족, 광고 과다 | 뉴스+커뮤니티 결합 |
| **한국일보** | ~500K | 뉴스 신뢰도 | 커뮤니티 없음 | 커뮤니티 차별화 |
| **HeyKorean** | ~200K | 커뮤니티+생활서비스 | UI 구식 | UX + 로컬 특화 |
| **Nextdoor** | - | 하이퍼로컬 | 한국어 없음 | 한인 특화 + 이중 언어 |
| **Reddit r/orangecounty** | - | 활발한 토론 | 한국어 없음 | 한인 커뮤니티 전용 |

### 1단계 목표: RadioKorea 추월 (월 600K+)
### 최종 목표: 남가주 한인 #1 플랫폼 (월 1M+)

---

## 10. KPI & 성공 지표

### Phase 1 (3개월) — "콘텐츠 기반 확보"
- [ ] 일일 크롤링 뉴스 100건 이상
- [ ] 월간 이벤트 200건 이상 수집
- [ ] 프로그래매틱 SEO 페이지 1,000개+
- [ ] 월간 방문자 10,000명
- [ ] Google 검색 노출 키워드 200개 이상
- [ ] 소셜 미디어 팔로워 합산 5,000명
- [ ] 페이지 체류 시간 3분 이상

### Phase 2 (6개월) — "커뮤니티 형성"
- [ ] 가입 회원 3,000명
- [ ] 월간 방문자 50,000명
- [ ] 일일 게시글 50건 이상
- [ ] 비즈니스 등록 500개 이상
- [ ] 월 광고 수입 $500 이상
- [ ] 앱 다운로드 1,000건+
- [ ] DAU/MAU 비율 20%+ (리텐션 지표)

### Phase 3 (12개월) — "RadioKorea 추월"
- [ ] **월간 방문자 600,000명** (RadioKorea 수준)
- [ ] 가입 회원 20,000명
- [ ] 일일 활성 사용자 5,000명
- [ ] 비즈니스 등록 2,000개 이상
- [ ] 월 수익 $3,000 이상
- [ ] 소셜 미디어 팔로워 합산 50,000명
- [ ] Google "OC Korean" 관련 키워드 1위

---

## 11. 예상 비용

### 초기 (Phase 1)
| 항목                      | 월 비용           | 비고                  |
| ----------------------- | -------------- | ------------------- |
| Vercel Pro              | $20            | Next.js 호스팅         |
| Supabase Pro            | $25            | DB + Auth + Storage |
| 크롤링 서버 (Railway)        | $10-30         | 사용량 기반              |
| AI API (Claude + DeepL) | $30-50         | 뉴스 요약 + 번역          |
| Upstash Redis           | $0-10          | 캐싱                  |
| Cloudflare              | $0             | CDN (Free Plan)     |
| 도메인                     | ~$12/년         | oc-lifehub.com      |
| **합계**                  | **~$85-135/월** |                     |

### 성장기 (Phase 2-3)
| 항목 | 월 비용 | 비고 |
|------|---------|------|
| Vercel Pro | $20 | |
| Supabase Pro | $25-75 | 트래픽 증가 |
| Railway | $30-50 | 크롤링 확대 |
| AI API | $50-100 | |
| Redis | $10-30 | |
| Cloudflare Pro | $20 | WAF + Analytics |
| 마케팅 | $100-300 | 소셜 광고 |
| **합계** | **~$255-575/월** | |

---

## 12. 실행 계획 (타임라인)

### Week 1-2: 기반 설정
- [ ] 도메인 구매 & DNS 설정
- [ ] Next.js 15 프로젝트 초기화 (App Router + TypeScript)
- [ ] Supabase 프로젝트 생성 & DB 스키마 구축
- [ ] 디자인 시스템 구축 (Tailwind + shadcn/ui)
- [ ] CI/CD 파이프라인 설정 (Vercel + GitHub)

### Week 3-4: 크롤링 파이프라인
- [ ] 뉴스 크롤러 개발 (RSS + Scrapy)
- [ ] 이벤트 크롤러 개발 (Eventbrite API + 웹)
- [ ] AI 요약 파이프라인 (Claude API)
- [ ] 자동 번역 파이프라인 (DeepL API)
- [ ] 크롤링 스케줄러 & 모니터링

### Week 5-6: 프론트엔드 MVP
- [ ] 뉴스 피드 페이지 (AI 요약 포함)
- [ ] 이벤트 캘린더 (인터랙티브 지도)
- [ ] 맛집 지도 페이지
- [ ] 통합 검색 기능
- [ ] 모바일 반응형 + PWA 설정

### Week 7-8: SEO & 프로그래매틱 페이지
- [ ] 도시별 자동 랜딩 페이지 생성
- [ ] SEO 최적화 (메타 태그, 사이트맵, 구조화 데이터)
- [ ] Google Search Console + Naver Webmaster 등록
- [ ] 소셜 미디어 계정 생성 & 콘텐츠 시작
- [ ] **베타 런칭** 🚀

### Week 9-12: 성장 & 커뮤니티
- [ ] 회원가입/로그인 시스템
- [ ] 게시판 MVP (자유/질문/벼룩시장)
- [ ] 포인트/레퍼럴 시스템
- [ ] 한인 업소 무료 등록 캠페인 시작
- [ ] 오프라인 홍보 시작 (교회, 마트, 축제)

---

## 13. 리스크 & 대응 방안

| 리스크 | 영향도 | 대응 방안 |
|--------|--------|-----------|
| 크롤링 차단 | 높음 | API 우선 사용, 다중 소스 확보, RSS 활용, 자체 콘텐츠 병행 |
| 저작권 이슈 | 높음 | AI 요약 + 출처 링크, Fair Use 준수, 뉴스 제휴 체결 |
| RadioKorea 반격 | 중간 | 기능 출시 속도로 선제 확보, 커뮤니티 충성도 구축 |
| 트래픽 성장 부족 | 중간 | SEO + 소셜 + 오프라인 3중 공격, 바이럴 콘텐츠 강화 |
| 커뮤니티 냉각 | 중간 | 시드 유저 확보, 초기 콘텐츠 직접 생산, 게이미피케이션 |
| 스팸/악성 게시물 | 중간 | AI 필터링 + 신고 시스템 + 관리자 모니터링 |
| 비용 증가 | 낮음 | 서버리스 아키텍처, 캐싱 최적화, 수익 재투자 |

---

## 14. 핵심 성공 요소

1. **콘텐츠 볼륨이 승부** — RadioKorea보다 더 많은 콘텐츠를 더 빠르게 (AI 파이프라인)
2. **UX가 무기** — 한 번 써보면 RadioKorea로 돌아갈 수 없게 만든다
3. **SEO가 엔진** — 프로그래매틱 SEO로 1,000개+ 키워드 장악
4. **커뮤니티가 해자(moat)** — 활성 커뮤니티는 경쟁자가 복제 불가
5. **모바일이 전장** — 20-50대 대부분 스마트폰 접속, 앱 수준 경험 필수
6. **오프라인 연결** — 온라인+오프라인 결합이 한인 커뮤니티 특성에 맞음
7. **속도가 생명** — 빠른 MVP → 데이터 기반 개선 → 빠른 기능 추가

---

> **다음 단계:** 프로젝트 저장소 생성 → Next.js 초기화 → 크롤링 프로토타입 개발 → 첫 번째 SEO 랜딩 페이지 생성
