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
    if ($candidate -eq "python") {
        $cmd = Get-Command python -ErrorAction SilentlyContinue
        if ($cmd) {
            $PythonExe = $cmd.Source
            break
        }
    } elseif (Test-Path $candidate) {
        $PythonExe = $candidate
        break
    }
}

if (-not $PythonExe) {
    throw "Python executable not found."
}

$today = Get-Date -Format "yyyy-MM-dd"
$dailyReport = Join-Path $RepoRoot ("reports\daily_brief\" + $today + "-daily-report.md")
$jiraPlan = Join-Path $RepoRoot ("reports\jira\" + $today + "-jira-plan.md")
$dashboard = Join-Path $RepoRoot ("reports\dashboard\" + $today + "-startup-dashboard.html")
$portfolioDashboard = Join-Path $RepoRoot ("reports\portfolio\" + $today + "-multi-project-dashboard.html")

& $PythonExe (Join-Path $RepoRoot "scripts\generate_multi_project_reports.py")

if ($OpenAfter -and (Test-Path $portfolioDashboard)) {
    Start-Process $portfolioDashboard
} elseif ($OpenAfter -and (Test-Path $dashboard)) {
    Start-Process $dashboard
} elseif ($OpenAfter -and (Test-Path $dailyReport)) {
    Start-Process notepad.exe $dailyReport
}
if ($OpenAfter -and -not (Test-Path $dashboard) -and (Test-Path $jiraPlan)) {
    Start-Process notepad.exe $jiraPlan
}
