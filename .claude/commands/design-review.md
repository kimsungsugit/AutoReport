---
description: Run design review on generated HTML dashboards
---

생성된 HTML 대시보드에 대한 디자인 리뷰를 실행합니다.

인자: $ARGUMENTS

## 실행 절차

`.claude/agents/design-reviewer.md`의 프롬프트를 역할로 채택하고, 아래를 수행하세요.

### Step 1: 대상 파일 선택
인자에 따라 분석 대상이 결정됩니다.

| 인자 | 동작 |
|---|---|
| (없음) | `reports/dashboard/`에서 가장 최신 HTML 1개 |
| `portfolio` | `reports/portfolio/`에서 가장 최신 HTML 1개 |
| `all` | `reports/dashboard/` + `reports/portfolio/` 폴더 안의 모든 `*.html` |
| 디렉토리 경로 | 그 폴더 안의 모든 `*.html` (재귀 X, 직접 자식만) |
| 파일 경로 | 해당 파일 1개 |

**다중 파일 처리**: 디렉토리/`all` 인자인 경우 `Glob` 도구로 `*.html`을 모두 수집한 뒤,
파일별로 Step 2~3을 반복 실행합니다. 빠진 파일이 없도록 처리한 파일 목록을 출력 첫 줄에 명시합니다.

### Step 2: 분석 실행
각 HTML 파일에 대해 `design-reviewer.md`에 정의된 5단계 분석을 수행합니다:
1. 구조 검사
2. CSS 일관성 검사 — `scripts/design_system.py`와 대조
3. 다크모드 호환성
4. 반응형 검사
5. 시각 개선 기회

### Step 3: 결과 출력
`design-reviewer.md`의 출력 템플릿을 따릅니다.

**단일 파일**: 그 파일 한 장만 출력합니다.

**다중 파일**: 파일별 섹션을 순서대로 출력하고, **마지막에 모든 파일을 합친 종합 Anchor 표** 한 장을 추가로 출력합니다.
이 종합 표가 `/fix-design`의 입력으로 사용됩니다.

```markdown
# 다중 파일 분석 시 마지막에 출력할 표 예시
## 종합 Anchor 표 (모든 파일 합집합)
| ID | Severity | Source File | target | lines | selector |
|----|----------|-------------|--------|-------|----------|
| C1 | Critical | reports/dashboard/2026-05-04-...html | scripts/design_system.py | L120-L132 | .regen-bar |
| W3 | Warning  | reports/portfolio/2026-05-04-...html | scripts/generate_multi_project_reports.py | L275-L320 | .portfolio-grid |
```

### Step 4: 후속 안내
- Critical/Warning 항목이 있으면 `/fix-design` 커맨드로 수정할 수 있다고 안내
- 다중 파일이면 `/fix-design all` 또는 항목 ID 지정(`/fix-design C1,W3`) 가능함을 안내
