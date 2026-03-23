param(
    [switch]$OpenAfter
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$PythonCandidates = @(
    (Join-Path $RepoRoot ".venv\Scripts\python.exe"),
    (Join-Path $RepoRoot "backend\venv\Scripts\python.exe"),
    "python"
)

$PythonExe = $null
foreach ($candidate in $PythonCandidates) {
    $resolved = $null
    if ($candidate -eq "python") {
        $cmd = Get-Command python -ErrorAction SilentlyContinue
        if ($cmd) {
            $resolved = $cmd.Source
        }
    } elseif (Test-Path $candidate) {
        $resolved = $candidate
    }

    if (-not $resolved) {
        continue
    }

    try {
        $check = & $resolved -c "import importlib.util; print('OK' if importlib.util.find_spec('google.genai') else 'MISSING')"
        if (($check | Out-String).Trim() -eq "OK") {
            $PythonExe = $resolved
            break
        }
    } catch {
        Write-RunLog ("Python candidate check failed: " + $resolved + " :: " + $_.Exception.Message)
    }
}

if (-not $PythonExe) {
    throw "Python executable not found."
}

$today = Get-Date -Format "yyyy-MM-dd"
$portfolioDashboard = Join-Path $RepoRoot ("reports\portfolio\" + $today + "-multi-project-dashboard.html")
$retryCount = 3
$retryDelaySeconds = 120
$startedAt = Get-Date
$failureDir = Join-Path $RepoRoot "reports\startup_status"
$failureHtml = Join-Path $failureDir ($today + "-startup-failed.html")
$retryCmd = Join-Path $failureDir "retry_startup_reports.cmd"
$runLog = Join-Path $failureDir ($today + "-startup-run.log")
$generatorScript = Join-Path $RepoRoot "scripts\generate_multi_project_reports.py"

New-Item -ItemType Directory -Path $failureDir -Force | Out-Null

function Write-RunLog {
    param(
        [string]$Message
    )

    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Path $runLog -Value $line -Encoding UTF8
}

function Write-RetryCommand {
    $cmdContent = @(
        "@echo off",
        "`"$PSHOME\powershell.exe`" -ExecutionPolicy Bypass -File `"$PSScriptRoot\run_startup_reports.ps1`" -OpenAfter"
    )
    New-Item -ItemType Directory -Path $failureDir -Force | Out-Null
    Set-Content -Path $retryCmd -Value $cmdContent -Encoding ASCII
}

function Write-FailureHtml {
    param(
        [string]$Message,
        [int]$Attempts
    )

    $html = @"
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Startup Report Retry Needed</title>
  <style>
    body { margin:0; font-family:"Segoe UI","Noto Sans KR",sans-serif; background:linear-gradient(180deg,#f8f3e9,#efe6d8); color:#17212b; }
    .wrap { max-width:920px; margin:0 auto; padding:36px 28px; }
    .card { background:#fffdf9; border:1px solid #ddd2c1; border-radius:28px; padding:28px; box-shadow:0 18px 40px rgba(23,33,43,.08); }
    h1 { margin:0 0 10px; font-size:34px; }
    p { line-height:1.6; }
    .meta { margin:18px 0; padding:16px; background:#fff7ed; border:1px solid #fed7aa; border-radius:18px; }
    code { background:#efe6d8; padding:2px 7px; border-radius:8px; }
    .action { display:inline-block; margin-top:12px; text-decoration:none; color:#0f4c5c; font-weight:700; background:#edf6f9; border:1px solid #c8d9dd; padding:12px 16px; border-radius:999px; }
  </style>
</head>
<body>
  <div class="wrap">
    <section class="card">
      <h1>자동 리포트 재시도 필요</h1>
      <p>시작 시 자동 리포트 실행이 여러 번 실패했습니다. 아래 안내에 따라 수동으로 다시 실행할 수 있습니다.</p>
      <div class="meta">
        <p><strong>재시도 횟수</strong>: $Attempts 회</p>
        <p><strong>재시도 간격</strong>: 120초</p>
        <p><strong>오류 메시지</strong>: <code>$Message</code></p>
      </div>
      <p>수동 재실행 파일: <code>$retryCmd</code></p>
      <a class="action" href="file:///$($retryCmd -replace '\\','/')">재시도 실행 파일 열기</a>
    </section>
  </div>
</body>
</html>
"@
    New-Item -ItemType Directory -Path $failureDir -Force | Out-Null
    Set-Content -Path $failureHtml -Value $html -Encoding UTF8
}

function Open-GeneratedHtmlReports {
    param(
        [datetime]$Since
    )

    $htmlFiles = Get-ChildItem -Path (Join-Path $RepoRoot "reports") -Recurse -Filter *.html -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -ge $Since.AddSeconds(-5) } |
        Sort-Object FullName -Unique

    Start-Sleep -Seconds 8

    $targetFile = $null
    if (Test-Path $portfolioDashboard) {
        $targetFile = Get-Item $portfolioDashboard
    } elseif ($htmlFiles) {
        $targetFile = $htmlFiles | Select-Object -First 1
    }

    if (-not $targetFile) {
        Write-RunLog "No HTML file found to open."
        return
    }

    $opened = $false
    for ($openAttempt = 1; $openAttempt -le 3; $openAttempt++) {
        try {
            Start-Process "explorer.exe" $targetFile.FullName
            Write-RunLog ("Opened startup dashboard: " + $targetFile.FullName)
            $opened = $true
            break
        } catch {
            Write-RunLog ("Open failed (" + $openAttempt + "/3): " + $targetFile.FullName + " :: " + $_.Exception.Message)
            Start-Sleep -Seconds 5
        }
    }

    if (-not $opened) {
        Write-RunLog ("Giving up opening startup dashboard: " + $targetFile.FullName)
    }
}

$lastErrorMessage = $null
$succeeded = $false
for ($attempt = 1; $attempt -le $retryCount; $attempt++) {
    try {
        Write-RunLog ("Startup report generation attempt " + $attempt)
        & $PythonExe $generatorScript
        if ($LASTEXITCODE -eq 0) {
            $succeeded = $true
            Write-RunLog "Report generation succeeded"
            break
        }
        $lastErrorMessage = "Generator exited with code $LASTEXITCODE"
        Write-RunLog $lastErrorMessage
    } catch {
        $lastErrorMessage = $_.Exception.Message
        Write-RunLog ("Generator exception: " + $lastErrorMessage)
    }

    if ($attempt -lt $retryCount) {
        Write-RunLog ("Retrying in " + $retryDelaySeconds + " seconds")
        Start-Sleep -Seconds $retryDelaySeconds
    }
}

if ($succeeded) {
    if ($OpenAfter) {
        Open-GeneratedHtmlReports -Since $startedAt
    }
    return
}

Write-RetryCommand
$failureMessage = if ($lastErrorMessage) { $lastErrorMessage } else { "Unknown startup report failure" }
Write-FailureHtml -Message $failureMessage -Attempts $retryCount
Write-RunLog ("Startup reports failed after retries: " + $failureMessage)

if ($OpenAfter -and (Test-Path $failureHtml)) {
    Start-Process $failureHtml
}

throw $failureMessage
