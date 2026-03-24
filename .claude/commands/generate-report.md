---
description: Generate daily/plan/jira reports for all or a specific project
---

AutoReport 리포트를 생성합니다.

## 사용법

프로젝트 이름 또는 "all"을 지정하세요:
- `/generate-report` — 모든 프로젝트 리포트 생성
- `/generate-report 260105` — 특정 프로젝트만 생성
- `/generate-report all 2026-03-20` — 특정 날짜로 전체 생성

## 실행 절차

1. `scripts/startup_projects.json`에서 프로젝트 목록을 확인합니다
2. 인자가 없거나 "all"이면 `scripts/generate_multi_project_reports.py`를 실행합니다
3. 특정 프로젝트명이 주어지면 해당 프로젝트만 `scripts/generate_periodic_reports.py`로 실행합니다
4. 결과 파일 경로와 성공/실패 여부를 보여줍니다

## 출력물

- `reports/daily_brief/YYYY-MM-DD-daily-report.md` — 일일 리포트
- `reports/plans/YYYY-MM-DD-next-plan.md` — 다음 작업 계획
- `reports/jira/YYYY-MM-DD-jira-status.md` — Jira 상태 문서
- `reports/dashboard/YYYY-MM-DD-startup-dashboard.html` — HTML 대시보드
- `reports/portfolio/YYYY-MM-DD-multi-project-dashboard.html` — 포트폴리오 대시보드

인자: $ARGUMENTS

위 설명을 바탕으로, 인자를 파싱하여 적절한 Python 스크립트를 Bash로 실행해 주세요.
날짜 인자가 없으면 오늘 날짜를 사용합니다.
실행 후 생성된 파일 목록과 상태를 간략하게 보여주세요.
