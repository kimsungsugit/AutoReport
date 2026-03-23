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
$output = Join-Path $RepoRoot ("reports\morning_brief\" + $today + "-morning-report.md")

& $PythonExe `
    (Join-Path $RepoRoot "scripts\generate_morning_report.py") `
    --repo $RepoRoot `
    --output $output `
    --stdout

if ($OpenAfter) {
    Start-Process notepad.exe $output
}
