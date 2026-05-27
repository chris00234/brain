---
tags:
  - claude-code
  - workflow
  - video
  - ppt
  - automation
created: '2026-03-22'
---
# Claude Code로 동영상 분석 후 PPT 만들기

## 개요

Claude Code를 활용하여 동영상을 분석하고, 결과를 스크린샷 + 텍스트로 저장한 뒤, PPT(PowerPoint)를 자동 생성하는 워크플로우.

---

## 전체 워크플로우

```
동영상 → 프레임 추출 → Claude Code 분석 → 결과 저장 → PPT 생성
```

---

## Step 1: 동영상에서 프레임(스크린샷) 추출

Claude Code는 동영상 파일을 직접 읽을 수 없다. 먼저 `ffmpeg`로 프레임을 이미지로 추출해야 한다.

### ffmpeg 설치

```bash
brew install ffmpeg
```

### 프레임 추출 명령어

```bash
# 1초마다 1장씩 추출
ffmpeg -i input_video.mp4 -vf "fps=1" frames/frame_%04d.png

# 10초마다 1장씩 추출 (긴 영상에 적합)
ffmpeg -i input_video.mp4 -vf "fps=1/10" frames/frame_%04d.png

# 특정 구간만 추출 (예: 1분~2분)
ffmpeg -i input_video.mp4 -ss 00:01:00 -to 00:02:00 -vf "fps=1" frames/frame_%04d.png
```

> **팁**: 영상 길이에 따라 fps 값을 조절한다. 1시간짜리 영상을 1fps로 하면 3600장이 나오므로, `fps=1/10` 또는 `fps=1/30` 정도가 적당하다.

---

## Step 2: Claude Code로 프레임 분석

Claude Code는 이미지 파일을 직접 읽을 수 있다 (멀티모달 지원).

### 방법 A: 직접 분석 요청

Claude Code에서 다음과 같이 요청:

```
frames/ 폴더에 있는 이미지들을 순서대로 읽고,
각 프레임의 내용을 요약해줘.
주요 장면 전환이나 핵심 내용을 정리해줘.
```

### 방법 B: 스크립트로 일괄 처리

Claude Code에게 분석 스크립트 작성을 요청:

```
frames/ 폴더의 이미지들을 순서대로 분석해서,
각 프레임별 설명을 JSON으로 results/analysis.json에 저장해줘.
```

출력 예시 (`analysis.json`):

```json
[
  {
    "frame": "frame_0001.png",
    "timestamp": "0:00:01",
    "description": "타이틀 화면 - 프로젝트 소개",
    "key_text": "AI 기반 데이터 분석 플랫폼",
    "is_key_frame": true
  },
  {
    "frame": "frame_0010.png",
    "timestamp": "0:00:10",
    "description": "아키텍처 다이어그램 설명",
    "key_text": "마이크로서비스 구조",
    "is_key_frame": true
  }
]
```

---

## Step 3: 결과를 스크린샷 + 텍스트로 저장

핵심 프레임과 분석 텍스트를 정리하여 저장:

```
Claude Code에게 요청:
"분석 결과에서 is_key_frame이 true인 것만 골라서,
스크린샷은 key_frames/ 폴더에 복사하고,
텍스트 요약은 results/summary.md로 정리해줘."
```

---

## Step 4: PPT 생성

### 방법 A: python-pptx 사용 (추천)

Claude Code에게 직접 요청:

```
analysis.json과 key_frames/ 폴더의 이미지를 사용해서
python-pptx로 PPT를 만들어줘.

요구사항:
- 첫 슬라이드: 제목 슬라이드
- 이후: 각 핵심 프레임마다 이미지 + 설명 텍스트 슬라이드
- 마지막: 요약 슬라이드
```

사전 설치:

```bash
pip install python-pptx
```

### 방법 B: Marp (마크다운 → PPT)

```bash
npm install -g @marp-team/marp-cli
```

Claude Code에게 요청:

```
분석 결과를 Marp 형식의 마크다운으로 만들어줘.
그리고 marp 명령어로 PPTX로 변환해줘.
```

변환 명령:

```bash
marp presentation.md --pptx -o output.pptx
```

### 방법 C: Google Slides API 연동

Google Slides API를 통해 직접 슬라이드를 생성할 수도 있다. 인증 설정이 필요하므로 복잡도가 높다.

---

## 한줄 요약 - 실전 명령 예시

Claude Code에서 이렇게 한 번에 요청 가능:

```
1. ffmpeg로 input_video.mp4에서 10초마다 프레임 추출해줘
2. 추출된 프레임들을 분석해서 핵심 장면을 골라줘
3. 핵심 장면 이미지 + 설명으로 python-pptx를 사용해서 PPT 만들어줘
```

---

## 주의사항

- **프레임 수 조절**: 너무 많은 프레임을 추출하면 분석 시간이 오래 걸린다. 핵심만 추출하도록 fps를 조절할 것.
- **이미지 크기**: 고해상도 프레임은 분석에 시간이 걸릴 수 있다. 필요시 리사이즈.
- **python-pptx 설치 필수**: PPT 생성 전에 `pip install python-pptx` 확인.
- **긴 영상**: 1시간 이상 영상은 구간을 나눠서 분석하는 것이 효율적.

---

## 필요 도구 정리

| 도구 | 설치 명령 | 용도 |
|------|-----------|------|
| ffmpeg | `brew install ffmpeg` | 동영상 → 이미지 추출 |
| python-pptx | `pip install python-pptx` | PPT 파일 생성 |
| marp-cli (선택) | `npm install -g @marp-team/marp-cli` | 마크다운 → PPT 변환 |
