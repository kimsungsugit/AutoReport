---
description: Run full QA validation on AutoReport pipeline
---

AutoReport 전체 파이프라인에 대한 QA 검증을 실행합니다.

인자: $ARGUMENTS

## 실행 절차

`.claude/agents/qa-engineer.md`의 프롬프트를 역할로 채택하고, 7단계 검증을 수행하세요.

### Phase 1: Import 검증
```bash
cd D:/Project/Program/AutoReport
python -c "
from scripts.design_system import DESIGN_CSS, CHECKLIST_JS
from scripts.generate_periodic_reports import run_git
from scripts.generate_multi_project_reports import load_projects
from scripts.auto_commit_push import render_html
print('All imports OK')
"
```

### Phase 2: 리포트 생성
```bash
python scripts/generate_periodic_reports.py --repo "D:/Project/Program/AutoReport" --output-root "D:/Project/Program/AutoReport/reports/projects/AutoReport" --profile reporting_automation
```
exit code와 생성된 파일 수 확인.

### Phase 3~5: HTML/CSS/다크모드 검증
생성된 HTML 파일들에 대해 `qa-engineer.md`에 정의된 검증 수행.

### Phase 6: 멀티 프로젝트 (인자에 "full"이 포함된 경우만)
```bash
python scripts/generate_multi_project_reports.py
```

### Phase 7: 회귀 비교
이전 날짜의 리포트가 있으면 파일 크기와 CSS 클래스를 비교.

### 결과 출력
`qa-engineer.md`에 정의된 출력 형식(Summary 테이블 + Verdict)으로 보고하세요.
FAIL이면 구체적 수정 방안을 제시하세요.
