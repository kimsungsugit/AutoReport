$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$PowerShellExe = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
$TargetScript = Join-Path $RepoRoot "scripts\run_evening_auto_commit_push.ps1"
$TaskName = "260105_AutoCommitPush_1700"
$UserId = "$env:USERDOMAIN\$env:USERNAME"

$action = New-ScheduledTaskAction -Execute $PowerShellExe -Argument "-ExecutionPolicy Bypass -File `"$TargetScript`""
$trigger = New-ScheduledTaskTrigger -Daily -At 5:00PM
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "Scheduled task created:"
Write-Host $TaskName
