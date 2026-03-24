---
description: Run design review on generated HTML dashboards
---

생성된 HTML 대시보드에 대한 디자인 리뷰를 실행합니다.

인자: $ARGUMENTS

## 실행 절차

`.claude/agents/design-reviewer.md`의 프롬프트를 역할로 채택하고, 아래를 수행하세요.

### Step 1: 대상 파일 선택
- 인자가 없으면 `reports/dashboard/`에서 가장 최신 HTML을 선택
- 인자로 파일 경로가 주어지면 해당 파일 사용
- "portfolio"가 인자이면 `reports/portfolio/`에서 최신 HTML 선택

### Step 2: 분석 실행
대상 HTML 파일을 읽고, `design-reviewer.md`에 정의된 5단계 분석을 수행합니다:
1. 구조 검사
2. CSS 일관성 검사 — `scripts/design_system.py`와 대조
3. 다크모드 호환성
4. 반응형 검사
5. 시각 개선 기회

### Step 3: 결과 출력
`design-reviewer.md`에 정의된 출력 형식(점수 포함)으로 결과를 보여주세요.

### Step 4: 후속 안내
Critical/Warning 항목이 있으면 `/fix-design` 커맨드로 수정할 수 있다고 안내하세요.
