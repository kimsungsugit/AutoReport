---
description: Full design improvement cycle - review, fix, validate (PM orchestrated)
---

디자인 개선 전체 사이클을 PM이 조율하여 실행합니다.

인자: $ARGUMENTS

## 실행 절차

`.claude/agents/pm.md`의 프롬프트를 역할로 채택하고, 팀을 조율하세요.

### Round 1: 현황 파악

**Design Reviewer 역할 수행:**
1. 최신 대시보드 HTML (`reports/dashboard/` 최신 파일)을 분석
2. `scripts/design_system.py` 현재 상태 확인
3. 5단계 디자인 리뷰 실행
4. Critical/Warning/Enhancement 목록 생성

사용자에게 리뷰 결과를 보여주고 수정 범위를 확인합니다.

### Round 2: 수정 실행

**Frontend Dev 역할 수행:**
인자 또는 사용자 확인에 따라:
- 인자가 없으면: Critical + Warning 전부 수정
- "critical"이면: Critical만 수정
- 구체적 지시가 있으면: 해당 항목만 수정

수정 순서:
1. `scripts/design_system.py` — CSS 변수/컴포넌트 먼저
2. HTML 렌더러 — 디자인 시스템 적용
3. SVG 함수 — 색상 참조 변경 (해당 시)

### Round 3: 검증

**QA Engineer 역할 수행:**
1. Import 검증
2. 리포트 재생성
3. HTML/CSS 매칭 검증
4. 다크모드 검증
5. 회귀 비교

### 판정
- **PASS**: 작업 완료 보고
- **PASS WITH WARNINGS**: Warning 목록과 함께 완료 보고
- **FAIL**: 실패 원인을 파악하여 Round 2로 돌아감 (최대 3회)

### 최종 보고
```markdown
## Design Improvement Report

### 작업 요약
- 리뷰 발견: Critical N개, Warning N개, Enhancement N개
- 수정 완료: N개
- QA 결과: PASS/FAIL

### 변경된 파일
- [파일]: [변경 요약]

### Before/After
- CSS 라인: N → N
- 다크모드: 미지원 → 지원
- hover 효과: N개 → N개
- etc.
```
