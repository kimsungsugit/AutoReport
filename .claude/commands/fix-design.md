---
description: Fix design issues found by design review
---

디자인 리뷰에서 발견된 문제를 수정합니다.

인자: $ARGUMENTS

## 실행 절차

`.claude/agents/frontend-dev.md`의 프롬프트를 역할로 채택하고, 아래를 수행하세요.

### Step 1: 수정 대상 확인
- 인자가 없으면: 먼저 `/design-review`를 내부적으로 실행하여 현재 이슈를 파악
- 인자로 구체적 지시가 있으면: 해당 내용을 수정 대상으로 사용
  - 예: `/fix-design 다크모드 색상` → 다크모드 관련 하드코딩 색상 수정
  - 예: `/fix-design Critical` → Critical 이슈만 수정

### Step 2: 수정 계획 수립
`frontend-dev.md`의 규칙을 준수하며:
1. 수정할 파일과 라인 범위를 특정
2. CSS 변경이 필요하면 `design_system.py` 먼저 수정
3. HTML 렌더링 함수 수정은 그 다음

### Step 3: 코드 수정
- `design_system.py`: CSS 변수, 컴포넌트 추가/수정
- HTML 렌더러: 하드코딩 제거, 새 클래스 적용
- **SVG 함수**: fill/stroke 색상은 Python 상수 참조

### Step 4: 즉시 검증
수정 후 바로:
```bash
python -c "from scripts.design_system import DESIGN_CSS; print('Import OK')"
python scripts/generate_periodic_reports.py --repo "D:/Project/Program/AutoReport" --output-root "D:/Project/Program/AutoReport/reports/projects/AutoReport" --profile reporting_automation
```

### Step 5: 결과 보고
`frontend-dev.md`에 정의된 출력 형식으로 변경 사항을 보고하세요.
QA가 필요하면 `/qa` 커맨드를 안내하세요.
