param(
  [Parameter(Mandatory = $true)][string]$SourceRoot,
  [Parameter(Mandatory = $true)][string]$ReqPaths,
  [string]$Template = "",
  [string]$ReportDir = "reports",
  [switch]$TestMode,
  [switch]$Full,
  [switch]$AiEnable,
  [switch]$Expand,
  [switch]$AiDetailed,
  [int]$RagTopK = 12,
  [string]$BaselineOut = "reports/uds_local/quality_baseline.json",
  [string]$RunOut = "reports/uds_local/quality_run.json",
  [string]$CompareOut = "reports/uds_local/quality_compare.json",
  [switch]$FailOnRegression
)

$py = "d:\Project\devops\260105\backend\venv\Scripts\python.exe"
$script = "d:\Project\devops\260105\scripts\uds\uds_quality_cycle.py"

$args = @(
  $script,
  "--source-root", $SourceRoot,
  "--req-paths", $ReqPaths,
  "--report-dir", $ReportDir,
  "--rag-top-k", "$RagTopK",
  "--baseline-out", $BaselineOut,
  "--run-out", $RunOut,
  "--compare-out", $CompareOut
)

if ($Template) { $args += @("--template", $Template) }
if ($TestMode) { $args += "--test-mode" }
if ($Full) { $args += "--full" }
if ($AiEnable) { $args += "--ai-enable" }
if ($Expand) { $args += "--expand" }
if ($AiDetailed) { $args += "--ai-detailed" }

& $py @args
if ($LASTEXITCODE -ne 0) {
  throw "UDS quality cycle failed with exit code $LASTEXITCODE"
}

if ($FailOnRegression -and (Test-Path $CompareOut)) {
  $cmp = Get-Content -Raw -Path $CompareOut | ConvertFrom-Json
  if ($cmp.soft_fail -eq $true) {
    $reason = ($cmp.soft_fail_reasons -join ", ")
    throw "UDS quality regression detected: $reason"
  }
}

