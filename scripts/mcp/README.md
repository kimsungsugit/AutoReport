# MCP 스크립트 실행 가이드

## 대상 스크립트
- `run_ui_checks.ps1` : 프론트 UI 검증 준비/체크
- `run_backend_checks.ps1` : 백엔드 API 검증

## 실행 준비
- PowerShell 실행 정책이 제한적이면: `Set-ExecutionPolicy -Scope Process Bypass`
- 백엔드/프론트가 실행 중인지 확인

## 실행 방법
PowerShell:
```
cd d:\Project\devops\260105\scripts\mcp
.\run_backend_checks.ps1
.\run_backend_checks.ps1 -BaseUrl http://127.0.0.1:8002
.\run_backend_checks.ps1 -EnableTestGen
.\run_backend_checks.ps1 -EnableAll
.\run_ui_checks.ps1 -UiUrl http://localhost:5173
```

## 참고
- 스크립트는 기본 API 응답을 점검하고 MCP 테스트 실행 전 상태를 확인합니다.
- MCP 실제 UI 제어는 `docs/mcp_test_guide.md` 절차를 따릅니다.
- 실행 로그는 `scripts/mcp/logs/`에 저장됩니다.

