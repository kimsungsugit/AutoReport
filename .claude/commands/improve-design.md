---
description: Full design improvement cycle - review, fix, validate (PM orchestrated)
---

디자인 개선 전체 사이클을 PM이 조율하여 실행합니다.

인자: $ARGUMENTS

## 실행 절차

`.claude/agents/pm.md`의 프롬프트를 역할로 채택하고, 팀을 조율하세요.

### Round 0: 분석 대상 결정
인자에 따라 분석 대상을 결정합니다.

| 인자 | 동작 |
|---|---|
| (없음) | `reports/dashboard/`에서 가장 최신 HTML 1개 |
| `portfolio` | `reports/portfolio/`에서 가장 최신 HTML 1개 |
| `all` | `reports/dashboard/` + `reports/portfolio/`의 모든 `*.html` |
| 디렉토리 경로 | 그 폴더 안의 모든 `*.html` |
| `critical`, `C1,W3` 등 | (사용자가 이미 리뷰 결과를 본 경우) 그 항목만 Round 2부터 시작 |

### Round 1: 현황 파악 (다중 파일 지원)

**Design Reviewer 역할 수행:**
1. Round 0에서 결정된 모든 대상 파일을 분석
2. `scripts/design_system.py` 현재 상태 확인
3. 파일별로 5단계 디자인 리뷰 실행
4. 파일별 Critical/Warning/Enhancement 목록 생성
5. **종합 Anchor 표** 생성 (모든 파일 합집합) — Round 2의 입력

사용자에게 종합 Anchor 표를 보여주고 수정 범위를 확인합니다.

### Round 2: 수정 실행 (Scope 규율)

**Frontend Dev 역할 수행:**
인자 또는 사용자 확인에 따라 처리할 항목 ID를 확정:
- 인자가 없으면: Critical + Warning 전부
- `critical`이면: Critical만
- `C1,W3` 형태이면: 지정 ID만

**수정 규율** (frontend-dev.md의 Scope Discipline 준수):
- 각 anchor의 `lines` 범위만 편집
- 범위 밖은 절대 변경하지 않음
- 같은 파일에 여러 anchor가 모이면 한 Edit으로 묶되, 변경 라인은 anchor 합집합 안에 있어야 함

수정 순서:
1. `scripts/design_system.py` — CSS 변수/컴포넌트 먼저 (새 클래스 추가는 범위 밖 허용 예외)
2. HTML 렌더러 — 디자인 시스템 적용
3. SVG 함수 — 색상 참조 변경 (해당 시)

**다중 파일 케이스**: 영향받는 모든 렌더러 함수를 한 라운드에 일괄 처리합니다.
파일별로 Round 2를 반복하지 않습니다.

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
- 분석 대상 파일: N개
- 리뷰 발견: Critical N개, Warning N개, Enhancement N개
- 처리 ID: C1, C2, W1, W3 (총 N개)
- 수정 완료: N개
- QA 결과: PASS/FAIL

### Scope 자가 점검
- 변경 라인 합계: N / anchor 범위 합계: M
- 범위 밖 변경: 0 (또는 N — 사유 첨부)

### 변경된 파일
- [파일]: [변경 요약]

### Before/After
- CSS 라인: N → N
- 다크모드: 미지원 → 지원
- hover 효과: N개 → N개
- etc.
```
