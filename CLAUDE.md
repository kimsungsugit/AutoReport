# AutoReport - Claude Code 프로젝트 컨텍스트

## 프로젝트 개요
여러 Git 프로젝트의 활동을 자동 수집하여 일일/주간/월간 리포트 + Jira 상태 문서 + HTML 대시보드를 자동 생성하는 도구.

## Python 환경
- **MCP 서버 전용**: `C:/Users/kss11/AppData/Local/Programs/Python/Python312/python.exe` (Windows 네이티브, mcp 패키지 설치됨)
- **일반 스크립트 실행**: 시스템 Python (`python`) — MSYS2 환경, google-genai/pydantic 등 설치됨
- venv는 사용하지 않음 (MSYS2에서 Rust 기반 패키지 빌드 불가)

## 디렉토리 구조
```
AutoReport/
├── scripts/
│   ├── generate_periodic_reports.py  # 핵심 엔진 (137KB, 2600+줄)
│   ├── generate_multi_project_reports.py  # 멀티 프로젝트 오케스트레이터
│   ├── generate_morning_report.py    # git 기반 아침 리포트
│   ├── auto_commit_push.py           # 저녁 자동 커밋/푸시
│   ├── generate_history_dashboard.py # 히스토리 대시보드
│   ├── design_system.py              # 공유 CSS 디자인 시스템 (SSoT)
│   ├── startup_projects.json         # 모니터링 대상 프로젝트 설정
│   └── mcp/
│       └── autoreport_mcp_server.py  # MCP 서버 (Claude Code 연동)
├── workflow/
│   └── llm_adapters.py               # LLM 어댑터 (Gemini/OpenAI/Anthropic)
├── config.py                          # OAI_CONFIG_LIST 로더
├── reports/                           # 생성된 리포트 (gitignored)
├── .claude/
│   ├── agents/                        # 에이전트 역할 정의
│   │   ├── pm.md                      # PM — 작업 계획/조율
│   │   ├── design-reviewer.md         # 디자인 리뷰어 — HTML/CSS 분석
│   │   ├── frontend-dev.md            # 프런트엔드 개발 — 코드 수정
│   │   └── qa-engineer.md             # QA — 검증/회귀 테스트
│   └── commands/                      # 슬래시 커맨드 (스킬)
└── project_docs/                      # 프로젝트 문서
```

## 핵심 규칙

### 대형 파일 규칙
- `generate_periodic_reports.py`는 **137KB / 2600+줄** — 절대 전체를 읽지 말 것
- 필요한 함수만 offset/limit으로 읽기 (함수 맵은 아래 참조)

### CSS 규칙
- 인라인 CSS 추가 금지 — `design_system.py`가 CSS의 **Single Source of Truth**
- 하드코딩 색상 금지 — CSS 변수(`var(--ink)`, `var(--accent)`) 사용
- 새 스타일이 필요하면 `design_system.py`에 추가 후 HTML에서 클래스로 참조

### generate_periodic_reports.py 함수 맵
| 함수 | 라인 | 설명 |
|---|---|---|
| `render_detail_html()` | L1337 | 상세 리포트 HTML |
| `render_html_dashboard()` | L2171 | 대시보드 HTML |
| `html_task_board()` | L2102 | Jira 태스크 보드 |
| `svg_area_bars()` | L1908 | 바 차트 SVG |
| `svg_flow()` | L1932 | 플로우 다이어그램 |
| `svg_structure_map()` | L1962 | 구조 맵 |
| `svg_action_roadmap()` | L1989 | 로드맵 |
| `svg_architecture_delta()` | L2014 | 아키텍처 델타 |
| `svg_change_impact_map()` | L2060 | 변경 영향 맵 |
| `build_context_payload()` | L642 | AI 분석 컨텍스트 빌드 |
| `ask_gemini_for_sections()` | L903 | Gemini AI 호출 |
| `generate_document()` | L1305 | 문서 생성 오케스트레이터 |
| `main()` | L2424 | 엔트리포인트 |

## 팀 에이전트 워크플로우

### 에이전트 역할
| 에이전트 | 프롬프트 | 역할 |
|---|---|---|
| PM | `.claude/agents/pm.md` | 작업 분해, 순서 결정, 팀 조율, 진행 보고 |
| Design Reviewer | `.claude/agents/design-reviewer.md` | HTML/CSS 5단계 분석, 점수화, 개선점 도출 |
| Frontend Dev | `.claude/agents/frontend-dev.md` | 코드 수정 (design_system → HTML 렌더러 순서) |
| QA Engineer | `.claude/agents/qa-engineer.md` | 7단계 검증 (import → 생성 → HTML → CSS → 다크모드 → 회귀) |

### 워크플로우
```
사용자 요청
    ↓
PM: 작업 분해 + 순서 결정
    ↓
Design Reviewer: 현재 상태 분석 → 이슈 목록
    ↓
Frontend Dev: 이슈 수정 (design_system.py 먼저 → HTML 렌더러)
    ↓
QA Engineer: 검증 (PASS → 완료 / FAIL → Frontend Dev로 복귀, 최대 3회)
    ↓
PM: 결과 보고
```

## 슬래시 커맨드

### 운영
| 커맨드 | 설명 |
|---|---|
| `/generate-report [project] [date]` | 리포트 생성 |
| `/dashboard` | 최신 대시보드 정보 |
| `/report-status [date]` | 리포트 상태 확인 |
| `/auto-commit` | 자동 커밋/푸시 (dry-run 기본) |
| `/add-project <name> <path> [profile]` | 프로젝트 추가 |

### 개발
| 커맨드 | 설명 |
|---|---|
| `/design-review [file\|portfolio]` | 디자인 리뷰 실행 |
| `/fix-design [scope]` | 디자인 이슈 수정 |
| `/qa [full]` | QA 검증 실행 |
| `/improve-design [scope]` | 전체 개선 사이클 (PM 조율) |

## 리포트 생성 테스트 명령
```bash
# 단일 프로젝트 (가장 빠름)
python scripts/generate_periodic_reports.py --repo "D:/Project/Program/AutoReport" --output-root "D:/Project/Program/AutoReport/reports/projects/AutoReport" --profile reporting_automation

# 전체 프로젝트
python scripts/generate_multi_project_reports.py

# 자동 커밋 점검
python scripts/auto_commit_push.py --dry-run
```
