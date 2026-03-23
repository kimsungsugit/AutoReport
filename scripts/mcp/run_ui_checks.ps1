Param(
  [string]$UiUrl = "http://localhost:5173",
  [string]$ApiUrl = "http://127.0.0.1:8000"
)

$now = Get-Date -Format "yyyyMMdd_HHmmss"
$logDir = Join-Path $PSScriptRoot "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logPath = Join-Path $logDir "ui_checks_$now.log"
$results = @()

function Write-Log {
  param([string]$Message, [string]$Level = "INFO")
  $line = "[{0}] {1}" -f $Level, $Message
  Add-Content -Path $logPath -Value $line
  if ($Level -eq "ERROR") {
    Write-Host $Message -ForegroundColor Red
  } elseif ($Level -eq "WARN") {
    Write-Host $Message -ForegroundColor Yellow
  } else {
    Write-Host $Message
  }
}

Write-Log "MCP UI Checks (pre-flight)" "INFO"
Write-Log "UI: $UiUrl" "INFO"
Write-Log "API: $ApiUrl" "INFO"

try {
  Invoke-RestMethod -Method GET -Uri "$ApiUrl/api/config/defaults" | Out-Null
  Write-Log "OK: backend reachable" "INFO"
  $results += "backend:ok"
} catch {
  Write-Log "FAIL: backend not reachable" "ERROR"
  $results += "backend:fail"
  exit 1
}

try {
  Invoke-RestMethod -Method GET -Uri $UiUrl | Out-Null
  Write-Log "OK: frontend reachable" "INFO"
  $results += "frontend:ok"
} catch {
  Write-Log "FAIL: frontend not reachable" "ERROR"
  $results += "frontend:fail"
  exit 1
}

$okCount = ($results | Select-String ":ok").Count
$failCount = ($results | Select-String ":fail").Count
Write-Log "Summary -> ok:$okCount fail:$failCount" "INFO"
Write-Log "Log file: $logPath" "INFO"
Write-Log "Next: use MCP servers for UI validation." "WARN"
Write-Log " - cursor-browser-extension" "WARN"
Write-Log " - cursor-ide-browser" "WARN"
Write-Log "Guide: docs/mcp_test_guide.md" "WARN"
