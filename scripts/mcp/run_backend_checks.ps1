Param(
  [string]$BaseUrl = "http://127.0.0.1:8000",
  [switch]$EnableTestGen,
  [switch]$EnableAll,
  [string]$TestGenExclude = ""
)

$now = Get-Date -Format "yyyyMMdd_HHmmss"
$logDir = Join-Path $PSScriptRoot "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logPath = Join-Path $logDir "backend_checks_$now.log"
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

Write-Log "MCP Backend Checks" "INFO"
Write-Log "BaseUrl: $BaseUrl" "INFO"

function Invoke-Json {
  param([string]$Method, [string]$Url, [object]$Body = $null)
  if ($Body -ne $null) {
    return Invoke-RestMethod -Method $Method -Uri $Url -Body ($Body | ConvertTo-Json) -ContentType "application/json"
  }
  return Invoke-RestMethod -Method $Method -Uri $Url
}

try {
  $defaults = Invoke-Json -Method GET -Url "$BaseUrl/api/config/defaults"
  Write-Log "OK: defaults" "INFO"
  $results += "defaults:ok"
} catch {
  Write-Log "FAIL: defaults" "ERROR"
  $results += "defaults:fail"
  exit 1
}

try {
  $options = Invoke-Json -Method GET -Url "$BaseUrl/api/config/options"
  Write-Log "OK: options" "INFO"
  $results += "options:ok"
} catch {
  Write-Log "FAIL: options" "ERROR"
  $results += "options:fail"
  exit 1
}

try {
  $sessions = Invoke-Json -Method GET -Url "$BaseUrl/api/sessions"
  Write-Log "OK: sessions" "INFO"
  $results += "sessions:ok"
} catch {
  Write-Log "FAIL: sessions" "ERROR"
  $results += "sessions:fail"
  exit 1
}

try {
  $session = Invoke-Json -Method POST -Url "$BaseUrl/api/sessions/new"
  Write-Log "OK: new session -> $($session.id)" "INFO"
  $results += "session_new:ok"
} catch {
  Write-Log "FAIL: new session" "ERROR"
  $results += "session_new:fail"
  exit 1
}

try {
  $data = Invoke-Json -Method GET -Url "$BaseUrl/api/sessions/$($session.id)/data"
  Write-Log "OK: session data" "INFO"
  $results += "session_data:ok"
} catch {
  Write-Log "FAIL: session data" "ERROR"
  $results += "session_data:fail"
  exit 1
}

try {
  $root = $defaults.project_root
  $cmakeCandidate = Join-Path $root "my_lin_gateway_251118_bakup"
  if (Test-Path (Join-Path $cmakeCandidate "CMakeLists.txt")) {
    $root = $cmakeCandidate
  }
  $runBody = @{
    project_root = $root
    config = $defaults
  }
  $runBody.config.project_root = $root
  if ($EnableAll) {
    $runBody.config.enable_test_gen = $true
    $runBody.config.auto_run_tests = $true
    $runBody.config.enable_agent = $true
    $runBody.config.do_docs = $true
    $runBody.config.do_coverage = $true
    $runBody.config.do_qemu = $false
    # Avoid pgvector hard-fail when DSN isn't configured
    $runBody.config.kb_storage = "sqlite"
    $runBody.config.pgvector_dsn = ""
    $runBody.config.pgvector_url = ""
    $runBody.config | Add-Member -NotePropertyName "test_gen_timeout_sec" -NotePropertyValue 60 -Force
    if ([string]::IsNullOrWhiteSpace($TestGenExclude)) {
      $runBody.config | Add-Member -NotePropertyName "test_gen_excludes" -NotePropertyValue @("e2e.c") -Force
    } else {
      $runBody.config | Add-Member -NotePropertyName "test_gen_excludes" -NotePropertyValue $TestGenExclude -Force
    }
    $runBody.config | Add-Member -NotePropertyName "test_gen_stub_only" -NotePropertyValue $true -Force
  } elseif (-not $EnableTestGen) {
    $runBody.config.enable_test_gen = $false
    $runBody.config.auto_run_tests = $false
    $runBody.config.enable_agent = $false
  }
  $run = Invoke-Json -Method POST -Url "$BaseUrl/api/sessions/$($session.id)/run" -Body $runBody
  Write-Log "OK: run requested" "INFO"
  $results += "run:ok"
} catch {
  Write-Log "WARN: run request failed" "WARN"
  $results += "run:warn"
}

$okCount = ($results | Select-String ":ok").Count
$failCount = ($results | Select-String ":fail").Count
$warnCount = ($results | Select-String ":warn").Count
Write-Log "Summary -> ok:$okCount fail:$failCount warn:$warnCount" "INFO"
Write-Log "Log file: $logPath" "INFO"
