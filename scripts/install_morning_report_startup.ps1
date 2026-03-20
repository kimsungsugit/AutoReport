$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$StartupDir = [Environment]::GetFolderPath("Startup")
$LauncherPath = Join-Path $StartupDir "260105_morning_report.cmd"
$PowerShellExe = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
$TargetScript = Join-Path $RepoRoot "scripts\run_startup_reports.ps1"

$content = @(
    "@echo off",
    "`"$PowerShellExe`" -ExecutionPolicy Bypass -File `"$TargetScript`" -OpenAfter"
)

Set-Content -Path $LauncherPath -Value $content -Encoding ASCII
Write-Host "Startup launcher created:"
Write-Host $LauncherPath
