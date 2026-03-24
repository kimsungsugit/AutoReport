---
description: Check report generation status for today or a specific date
---

오늘(또는 지정 날짜)의 리포트 생성 상태를 확인합니다.

인자: $ARGUMENTS

아래 단계를 수행해 주세요:

1. 인자로 날짜(YYYY-MM-DD)가 주어지면 해당 날짜, 없으면 오늘 날짜를 사용합니다
2. 다음 디렉토리에서 해당 날짜의 파일을 검색합니다:
   - `reports/daily_brief/`
   - `reports/plans/`
   - `reports/jira/`
   - `reports/dashboard/`
   - `reports/portfolio/`
   - `reports/weekly_brief/`
   - `reports/monthly_brief/`
3. `reports/projects/` 아래 각 프로젝트별로도 해당 날짜의 파일을 확인합니다
4. 각 카테고리별로 파일 존재 여부와 크기를 표로 정리합니다
5. 누락된 리포트가 있으면 `/generate-report`으로 생성할 수 있다고 안내합니다
