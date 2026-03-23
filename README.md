# DevOps Analysis Toolkit

SRS/SDS 문서와 C 소스 코드로부터 **UDS(Unit Design Specification)**, **STS(Software Test Specification)**, **SUTS(Software Unit Test Specification)** 문서를 자동 생성하는 통합 분석 플랫폼입니다.

## 아키텍처

```
┌─────────────────────────────────────────────┐
│              React Frontend (Vite)          │
│  Views: JenkinsWorkflow / LocalWorkflow     │
│         LocalEditor / LocalDashboard        │
│  Context: Chat / UDS / JenkinsConfig        │
└────────────────┬────────────────────────────┘
                 │ REST API + SSE
┌────────────────▼────────────────────────────┐
│           FastAPI Backend (:7000)            │
│  Routers: chat / excel / jenkins / local    │
│           vcast / qac / test_gen / impact   │
│           sessions / profiles / exports     │
└────────────────┬────────────────────────────┘
                 │
    ┌────────────┼────────────────┐
    ▼            ▼                ▼
┌────────┐ ┌──────────┐ ┌──────────────┐
│report_gen│ │generators│ │  workflow/    │
│(UDS 생성)│ │(STS/SUTS)│ │(AI·빌드·분석)│
└────────┘ └──────────┘ └──────────────┘
                 │
                 ▼
         Gemini 3 Pro / 2.5 Flash
```

## 주요 기능

- **UDS 자동 생성**: C 소스 → 함수 분석 → AI 설명 강화 → DOCX 문서 생성
- **STS 자동 생성**: SRS 요구사항 → 테스트 케이스 → XLSM 문서 생성
- **SUTS 자동 생성**: UDS 함수 정보 → 단위 테스트 시퀀스 → XLSM 문서 생성
- **Jenkins CI/CD 연동**: 빌드 로그 수집, 정적 분석(QAC), 커버리지 리포트
- **C 테스트 생성**: 단위 테스트 코드 자동 생성 (CMake/CTest/gcovr)
- **VectorCAST 리포트 파싱**: 테스트 커버리지 추적
- **AI 챗봇**: 프로젝트 맥락 기반 질의응답 (RAG)
- **문서 품질 검증**: Quality Gate, 추적성 매트릭스, 신뢰도 리포트

## 설치 및 실행

### 사전 요구사항

- Python 3.10+
- Node.js 18+
- (선택) Docker

### Backend 설정

```bash
# 가상환경 생성 및 활성화
python -m venv venv
venv\Scripts\activate      # Windows
# source venv/bin/activate # Linux/Mac

# 의존성 설치
pip install -r requirements.txt

# 환경변수 설정
copy .env.example .env
# .env 파일을 편집하여 GOOGLE_API_KEY 등을 설정하세요

# 서버 실행
uvicorn backend.main:app --host 0.0.0.0 --port 7000 --reload
```

### Frontend 설정

```bash
cd frontend
npm install
npm run dev    # 개발 서버 (http://localhost:5173)
npm run build  # 프로덕션 빌드
```

### Docker 실행

```bash
docker build -t devops-toolkit .
docker run -p 7000:7000 -e GOOGLE_API_KEY=your-key devops-toolkit
```

## 환경변수

`.env.example` 파일을 참조하여 `.env` 파일을 생성하세요.

| 변수 | 설명 | 기본값 |
|---|---|---|
| `GOOGLE_API_KEY` | Google AI (Gemini) API 키 | (필수) |
| `DEVOPS_PROJECT_ROOT` | 분석 대상 C 프로젝트 루트 경로 | `/app/my_lin_gateway` |
| `DEVOPS_JENKINS_BASE_URL` | Jenkins 서버 URL | `http://localhost:8080` |
| `DEVOPS_JENKINS_USERNAME` | Jenkins 사용자명 | `admin` |
| `DEVOPS_JENKINS_API_TOKEN` | Jenkins API 토큰 | (필수 - Jenkins 사용 시) |
| `LLM_GEMINI_ONLY` | Gemini 전용 모드 | `1` |
| `KB_STORAGE` | RAG 저장소 (sqlite/pgvector) | `sqlite` |

## 디렉토리 구조

```
260105/
├── backend/              # FastAPI 백엔드
│   ├── main.py           # 앱 진입점 + 미들웨어
│   ├── schemas.py        # Pydantic 요청/응답 모델
│   ├── helpers.py        # 공통 헬퍼 함수
│   └── routers/          # API 라우터 (chat, excel, jenkins, ...)
├── frontend/             # React + Vite 프론트엔드
│   └── src/
│       ├── App.jsx       # 메인 앱 컴포넌트
│       ├── views/        # 페이지 뷰 (JenkinsWorkflow, LocalWorkflow, ...)
│       ├── components/   # 재사용 컴포넌트
│       └── contexts/     # React Context (Chat, UDS, JenkinsConfig)
├── report_gen/           # UDS 생성 엔진 (8 모듈)
│   ├── source_parser.py  # C 소스 파싱
│   ├── function_analyzer.py  # 함수 분석
│   ├── requirements.py   # 요구사항 매핑
│   ├── uds_text.py       # UDS 텍스트 처리
│   ├── uds_generator.py  # UDS 미리보기/섹션 생성
│   ├── docx_builder.py   # DOCX 문서 조립
│   ├── validation.py     # 품질 검증
│   └── utils.py          # 유틸리티
├── generators/           # 문서 생성기
│   ├── sts.py            # STS (Software Test Specification)
│   └── suts.py           # SUTS (Software Unit Test Specification)
├── workflow/             # 핵심 워크플로우 엔진
│   ├── ai.py             # LLM 호출 (Gemini)
│   ├── uds_ai.py         # UDS AI 강화
│   ├── build.py          # C 빌드/테스트
│   ├── common.py         # 공통 유틸리티
│   └── gui_utils.py      # GUI 워크플로우 오케스트레이터
├── report/               # 리포트 상수/파싱
├── templates/            # DOCX/XLSM 템플릿
├── tests/                # 테스트
├── config.py             # 중앙 설정
├── Dockerfile            # Docker 빌드
├── Jenkinsfile           # CI/CD 파이프라인
└── OAI_CONFIG_LIST       # LLM 설정 (API 키는 환경변수 참조)
```

## API 엔드포인트 요약

| 경로 | 설명 |
|---|---|
| `GET /api/health` | 서버 상태 확인 |
| `POST /api/chat` | AI 챗봇 질의 |
| `POST /api/run/stop` | 실행 중인 작업 중단 |
| `/api/jenkins/*` | Jenkins 빌드/리포트 관련 |
| `/api/local/*` | 로컬 프로젝트 분석/리포트 |
| `/api/excel/*` | Excel 비교/변환 |
| `/api/vcast/*` | VectorCAST 리포트 파싱 |
| `/api/qac/*` | QAC 정적 분석 리포트 |
| `/api/test-gen/*` | C 테스트 코드 생성 |
| `/api/impact/*` | 변경 영향 분석 |
| `/api/sessions/*` | 세션 관리 |
| `/api/profiles/*` | 프로젝트 프로파일 관리 |
| `/api/exports/*` | 문서 내보내기 |

## 라이선스

내부 프로젝트 - 비공개
"# devops" 

## GitHub Project Documents

Trackable project documents for GitHub are stored in [`project_docs/`](/D:/Project/devops/260105/project_docs/README.md).

Recommended locations:

- Daily reports: [`project_docs/daily_reports/`](/D:/Project/devops/260105/project_docs/daily_reports/README.md)
- Weekly reports: [`project_docs/weekly_reports/`](/D:/Project/devops/260105/project_docs/weekly_reports/README.md)
- Change history: [`project_docs/change_history/`](/D:/Project/devops/260105/project_docs/change_history/README.md)
- Design docs: [`project_docs/design/`](/D:/Project/devops/260105/project_docs/design/README.md)
- Change requests: [`project_docs/change_requests/`](/D:/Project/devops/260105/project_docs/change_requests/README.md)

## Startup Reports

To generate startup reports manually:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_startup_reports.ps1
```

To generate and open the daily report automatically in Notepad:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_startup_reports.ps1 -OpenAfter
```

To install it into Windows Startup:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_morning_report_startup.ps1
```

Generated output:

- `reports/daily_brief/YYYY-MM-DD-daily-report.md`
- `reports/plans/YYYY-MM-DD-next-plan.md`
- `reports/jira/YYYY-MM-DD-jira-plan.md`
- `reports/jira/YYYY-MM-DD-jira-result.md`
- `reports/dashboard/YYYY-MM-DD-startup-dashboard.html`
- `reports/weekly_brief/YYYY-MM-DD-weekly-report.md` on Friday mornings
- `reports/monthly_brief/YYYY-MM-monthly-report.md` on the first Monday after month end

Optional integrations:

- `GITHUB_TOKEN`: enrich reports with GitHub API commit / PR metadata
- `GOOGLE_API_KEY` or `OAI_CONFIG_LIST`: generate higher-quality Korean summaries with Gemini
- Jira-ready documents are generated under `reports/jira/` for upload or copy/paste into Jira issues
