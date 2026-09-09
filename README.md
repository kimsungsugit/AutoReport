# AutoReport

여러 Git 프로젝트의 활동을 자동 수집하여 **일일/주간/월간 리포트**, **Jira 상태 문서**, **HTML 대시보드**, **포트폴리오 대시보드**를 자동 생성하는 도구입니다.

## 주요 기능

- **멀티 프로젝트 분석**: `scripts/startup_projects.json`에 등록된 프로젝트들의 git 활동을 자동 분석
- **AI 기반 인사이트**: Gemini로 일일 진행 요약, 다음 작업 계획, 변경 영향 분석 생성
- **Jira 연동**: APPL 보드의 스프린트/이슈/큰틀(Epic)/작업(Task) 라이브 표시, 인라인 큰틀·작업 생성
- **커밋 기반 Jira 계획**: 전체 SHA·변경 파일·테스트 근거로 계획을 작성하고 중복 방지된 Task 제안 생성
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
│   ├── sync_commit_to_jira.py           # 현재 커밋 → 계획/Jira proposal
│   ├── install_git_hooks.ps1            # 등록 프로젝트 post-commit hook 설치
│   ├── generate_history_dashboard.py   # 히스토리 대시보드
│   ├── design_system.py                # 공유 CSS/JS (Single Source of Truth)
│   ├── jira_proxy.py                   # Jira 라이브 API 프록시 (port 18923)
│   ├── startup_projects.json           # 모니터링 대상 프로젝트 설정
│   └── mcp/
│       └── autoreport_mcp_server.py    # MCP 서버 (Claude Code 연동)
├── workflow/
│   ├── llm_adapters.py                 # Gemini/OpenAI/Anthropic 어댑터
│   ├── jira_planning.py                 # 커밋 근거 → 구조화 계획
│   ├── jira_outbox.py                   # Jira write 영속 상태/복구
│   └── task_provider.py                 # Jira/내부 task provider
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
- (선택) Jira Server/Data Center 인스턴스 + Bearer PAT

## 환경변수

`.env` 파일을 프로젝트 루트에 생성하세요:

| 변수 | 설명 |
|---|---|
| `GOOGLE_API_KEY` | Gemini API 키 (AI 분석에 사용) |
| `JIRA_URL` | Jira 인스턴스 URL (예: `https://jira.example.com`) |
| `JIRA_TOKEN` | Jira PAT (Bearer Token) |
| `JIRA_PROJECT_KEY` | 기본 Jira 프로젝트 키 |
| `JIRA_VERIFY_TLS` | 인증서/호스트 검증 여부. 기본 `1` |
| `JIRA_AUTO_APPLY` | `0`: proposal만 저장, `1`: 검증 후 Jira Task 생성 |
| `JIRA_QUALITY_ROLLOUT_STAGE` | 품질 롤아웃 단계. `A`: 테스트, `B`: 섀도(쓰기 없음, 기본), `C`: 수동 승인만, `D`: 품질 통과 자동 적용 |

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

### 커밋 직후 계획/Jira proposal 생성

등록 프로젝트에 공용 `post-commit` hook을 설치합니다.

```powershell
.\scripts\install_git_hooks.ps1 -AllConfigured
```

한 저장소에만 설치하려면 `-TargetRepo "D:\Project\MyProject"`를 사용합니다.
기존 `core.hooksPath`가 있으면 그 경로와 기존 hook을 보존하고 AutoReport
`post-commit` 체인만 추가합니다. 이미 별도 `post-commit`이 있으면 안전하게
중단하며, 직접 병합하거나 검토 후에만 `-Force`를 사용합니다.

현재 커밋만 수동 점검할 수도 있습니다.

```bash
python scripts/sync_commit_to_jira.py --repo . --commit HEAD
```

기본값 `JIRA_AUTO_APPLY=0`에서는 `reports/jira_queue/`에 검토용 JSON만
저장됩니다. `JIRA_AUTO_APPLY=1`이어도 품질 롤아웃 기본 단계 `B`에서는
Jira 쓰기가 차단됩니다. 골든 테스트와 섀도 평가를 통과한 뒤 `C`에서
수동 승인 표본을 검증하고, 마지막에만 `D`로 승격하세요. 상세 계약은
[`project_docs/commit_to_jira_workflow.md`](project_docs/commit_to_jira_workflow.md)를
참조하세요.

제안 JSON의 코호트 품질은 Jira 쓰기 없이 다음 명령으로 확인합니다.

```bash
python scripts/evaluate_jira_suggestion_quality.py \
  reports/projects/Release_claude/reports/jira/2026-08-11-jira-suggestions.json \
  reports/projects/CyberSecurity/reports/jira/2026-08-11-jira-suggestions.json
```

목표와 단계별 승격 조건은
[`project_docs/jira_suggestion_quality_improvement_plan.md`](project_docs/jira_suggestion_quality_improvement_plan.md)에
정의돼 있습니다.

hook 결과는 프로젝트별 `post-commit-hook.last-exit`에 기록되고 자동
Commit/Push 상태 JSON·HTML의 `jira_sync_status`에도 노출됩니다. `failed`는
같은 HEAD를 다음 실행에서 다시 동기화한 뒤 push하며, `review_required`는 코드
push를 막지 않고 사람이 검토할 proposal을 남깁니다.

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
- 커밋/계획 근거가 포함된 `New Task` 제안 검토·편집·승인
- proposal ID summary marker와 영속 outbox를 통한 중복 생성 방지
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
