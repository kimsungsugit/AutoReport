---
description: Fix design issues found by design review
---

디자인 리뷰에서 발견된 문제를 수정합니다.

인자: $ARGUMENTS

## 실행 절차

`.claude/agents/frontend-dev.md`의 프롬프트를 역할로 채택하고, 아래를 수행하세요.

### Step 1: 수정 대상 확인 (Anchor 입력)
**원칙**: 이 커맨드는 Design Review의 **Anchor 표**를 계약으로 받아 그 범위만 수정합니다.

| 인자 | 동작 |
|---|---|
| (없음) | 먼저 `/design-review`를 내부 호출하여 Anchor 표를 생성한 뒤 모든 Critical+Warning 처리 |
| `Critical` | 가장 최근 리뷰 결과 중 Critical 항목만 처리 |
| `C1,W3,W5` | 지정한 ID 항목만 처리 (콤마 구분) |
| `all` | 가장 최근 다중 파일 리뷰의 종합 Anchor 표 전체 처리 |
| 자유 텍스트 | 텍스트와 가장 일치하는 항목을 골라 처리 (예: "다크모드 색상") |

리뷰 결과가 직전 메시지에 없거나 명확하지 않으면 **반드시 `/design-review`를 먼저 실행**하여 Anchor 표를 확보합니다.

### Step 2: 수정 계획 수립
`frontend-dev.md`의 **수정 범위 규율** 섹션을 준수하며:
1. 처리할 항목 ID 목록 확정 (예: C1, W2, W4)
2. 항목별 anchor의 `target` / `lines` 만 Read (offset/limit 사용)
3. 같은 파일에 여러 anchor가 있으면 라인 범위의 합집합을 한 번에 Read
4. CSS 변경이 필요하면 `design_system.py` 먼저 수정 → 그 다음 HTML 렌더러

### Step 3: 코드 수정
**anchor `lines` 범위 밖은 절대 편집하지 않습니다.**
- `design_system.py`: CSS 변수, 컴포넌트 추가/수정 (새 클래스 추가는 범위 밖이라도 허용된 예외)
- HTML 렌더러: 하드코딩 제거, 새 클래스 적용
- **SVG 함수**: fill/stroke 색상은 Python 상수 참조

### Step 4: 즉시 검증
수정 후 바로:
```bash
python -c "from scripts.design_system import DESIGN_CSS; print('Import OK')"
python scripts/generate_periodic_reports.py --repo "D:/Project/Program/AutoReport" --output-root "D:/Project/Program/AutoReport/reports/projects/AutoReport" --profile reporting_automation
```

### Step 5: Scope 자가 점검
`git diff --stat` 및 `git diff`로 변경 라인이 anchor 범위 안에 있는지 확인합니다.
- 범위 밖 변경이 발견되면 보고서에 사유 명시 또는 되돌림
- 다중 파일 케이스에서는 파일별로 자가 점검 결과를 표로 정리

### Step 6: 결과 보고
`frontend-dev.md`에 정의된 **Scope 자가 점검 표를 포함한** 출력 형식으로 변경 사항을 보고하세요.
QA가 필요하면 `/qa` 커맨드를 안내하세요.
