param()

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$today = Get-Date -Format "yyyy-MM-dd"
$statusDir = Join-Path $RepoRoot "reports\automation_status"
$runLog = Join-Path $statusDir ($today + "-auto-commit-push.log")
$scriptPath = Join-Path $RepoRoot "scripts\auto_commit_push.py"
$retryCmd = Join-Path $statusDir "retry_auto_commit_push.cmd"
$retryCount = 3
$retryDelaySeconds = 60

New-Item -ItemType Directory -Path $statusDir -Force | Out-Null

function Write-RunLog {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Path $runLog -Value $line -Encoding UTF8
}

function Write-RetryCommand {
    $cmdContent = @(
        "@echo off",
        "`"$PSHOME\powershell.exe`" -ExecutionPolicy Bypass -File `"$PSScriptRoot\run_evening_auto_commit_push.ps1`""
    )
    Set-Content -Path $retryCmd -Value $cmdContent -Encoding ASCII
}

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
        if ($cmd) { $resolved = $cmd.Source }
    } elseif (Test-Path $candidate) {
        $resolved = $candidate
    }
    if ($resolved) {
        $PythonExe = $resolved
        break
    }
}

if (-not $PythonExe) {
    throw "Python executable not found."
}

Write-RetryCommand

$lastErrorMessage = $null
for ($attempt = 1; $attempt -le $retryCount; $attempt++) {
    try {
        Write-RunLog ("Auto commit/push attempt " + $attempt)
        & $PythonExe $scriptPath --date $today
        if ($LASTEXITCODE -eq 0) {
            Write-RunLog "Auto commit/push succeeded"
            exit 0
        }
        $lastErrorMessage = "auto_commit_push.py exited with code $LASTEXITCODE"
        Write-RunLog $lastErrorMessage
    } catch {
        $lastErrorMessage = $_.Exception.Message
        Write-RunLog ("Auto commit/push exception: " + $lastErrorMessage)
    }

    if ($attempt -lt $retryCount) {
        Write-RunLog ("Retrying in " + $retryDelaySeconds + " seconds")
        Start-Sleep -Seconds $retryDelaySeconds
    }
}

if ($lastErrorMessage) {
    throw $lastErrorMessage
}

throw "Auto commit/push failed"
