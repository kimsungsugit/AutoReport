# AutoReport

여러 Git 프로젝트의 활동을 자동 수집하여 **일일/주간/월간 리포트**, **Jira 상태 문서**, **HTML 대시보드**, **포트폴리오 대시보드**를 자동 생성하는 도구입니다.

## 주요 기능

- **멀티 프로젝트 분석**: `scripts/startup_projects.json`에 등록된 프로젝트들의 git 활동을 자동 분석
- **AI 기반 인사이트**: Gemini로 일일 진행 요약, 다음 작업 계획, 변경 영향 분석 생성
- **Jira 연동**: APPL 보드의 스프린트/이슈/큰틀(Epic)/작업(Task) 라이브 표시, 인라인 큰틀·작업 생성
- **자동 커밋/푸시**: 매일 17:00에 등록된 repo들의 변경 사항 자동 커밋·푸시 (Windows Scheduled Task)
- **모닝 리포트**: Windows 로그인 시 자동 생성·대시보드 오픈
- **포트폴리오 대시보드**: 모든 프로젝트를 한 화면에서 비교

## 디렉토리 구조

```
AutoReport/
├── scripts/
│   ├── generate_periodic_reports.py    # 핵심 엔진 (단일 프로젝트)
│   ├── generate_multi_project_reports.py  # 멀티 프로젝트 오케스트레이터
│   ├── generate_morning_report.py      # 모닝 리포트
│   ├── auto_commit_push.py             # 자동 커밋/푸시
│   ├── generate_history_dashboard.py   # 히스토리 대시보드
│   ├── design_system.py                # 공유 CSS/JS (Single Source of Truth)
│   ├── jira_proxy.py                   # Jira 라이브 API 프록시 (port 18923)
│   ├── startup_projects.json           # 모니터링 대상 프로젝트 설정
│   └── mcp/
│       └── autoreport_mcp_server.py    # MCP 서버 (Claude Code 연동)
├── workflow/
│   ├── llm_adapters.py                 # Gemini/OpenAI/Anthropic 어댑터
│   └── task_provider.py                # Jira/내부 task provider
├── reports/                            # 생성된 리포트 (gitignored)
│   ├── projects/<name>/                # 프로젝트별 리포트
│   ├── portfolio/                      # 멀티 프로젝트 대시보드
│   ├── automation_status/              # 자동 커밋 상태
│   ├── jira/                           # Jira 상태 문서
│   └── history/                        # 히스토리 대시보드
├── .claude/
│   ├── agents/                         # PM / Design Reviewer / Frontend Dev / QA
│   └── commands/                       # 슬래시 커맨드
└── project_docs/                       # 프로젝트 문서
```

## 사전 요구사항

- Python 3.10+ (MCP 서버는 Python 3.12)
- Git (각 모니터링 대상 프로젝트는 git repo여야 함)
- (선택) Jira Server / Cloud 인스턴스 + PAT

## 환경변수

`.env` 파일을 프로젝트 루트에 생성하세요:

| 변수 | 설명 |
|---|---|
| `GOOGLE_API_KEY` | Gemini API 키 (AI 분석에 사용) |
| `JIRA_URL` | Jira 인스턴스 URL (예: `https://jira.example.com`) |
| `JIRA_TOKEN` | Jira PAT (Bearer Token) |

## 사용법

### 단일 프로젝트 리포트 생성

```bash
python scripts/generate_periodic_reports.py \
  --repo "D:/Project/Program/AutoReport" \
  --output-root "D:/Project/Program/AutoReport/reports/projects/AutoReport" \
  --profile reporting_automation
```

### 멀티 프로젝트 (모든 등록 프로젝트)

```bash
python scripts/generate_multi_project_reports.py
```

생성 결과: `reports/portfolio/YYYY-MM-DD-multi-project-dashboard.html`

### 자동 커밋 점검 (dry-run)

```bash
python scripts/auto_commit_push.py --dry-run
```

### 프로젝트 추가

`scripts/startup_projects.json`에 항목 추가:

```json
{
  "name": "MyProject",
  "path": "D:/Project/MyProject",
  "profile": "general_software",
  "enabled": true
}
```

지원 프로파일: `reporting_automation`, `desktop_app`, `general_software`, `uds_quality`

## Windows 자동화

### 매일 17:00 자동 커밋

```powershell
.\scripts\install_evening_auto_commit_task.ps1
```

→ `AutoReport_AutoCommitPush_1700` 작업이 Task Scheduler에 등록됨.

### 로그인 시 모닝 리포트

```powershell
.\scripts\install_morning_report_startup.ps1
```

→ `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\AutoReport_morning_report.cmd` 생성.

## Jira 라이브 보드

`scripts/jira_proxy.py`가 18923 포트로 실행되면 portfolio dashboard에서:

- 스프린트 보드 인라인 표시
- **큰틀(Epic)** / **작업(Task)** 인라인 생성
- 작업 → 큰틀 부모 선택, 일정·주간보고 필드 자동 상속
- 상태 전환 (진행 중 / 종료 요청)

```bash
python scripts/jira_proxy.py
```

## 슬래시 커맨드 (Claude Code)

| 커맨드 | 설명 |
|---|---|
| `/generate-report [project] [date]` | 리포트 생성 |
| `/dashboard` | 최신 대시보드 정보 |
| `/report-status [date]` | 리포트 상태 확인 |
| `/auto-commit` | 자동 커밋/푸시 (dry-run 기본) |
| `/add-project <name> <path> [profile]` | 프로젝트 추가 |
| `/design-review [file]` | 디자인 리뷰 실행 |
| `/qa [full]` | QA 검증 실행 |
| `/improve-design [scope]` | 전체 개선 사이클 (PM 조율) |

## 라이선스

내부 프로젝트 — 비공개.
