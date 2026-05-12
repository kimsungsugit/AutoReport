$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$PowerShellExe = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
$TargetScript = Join-Path $RepoRoot "scripts\run_evening_auto_commit_push.ps1"
$TaskName = "AutoReport_AutoCommitPush_1700"
$UserId = "$env:USERDOMAIN\$env:USERNAME"

$action = New-ScheduledTaskAction -Execute $PowerShellExe -Argument "-ExecutionPolicy Bypass -File `"$TargetScript`""
$trigger = New-ScheduledTaskTrigger -Daily -At 5:00PM
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
# LogonType=S4U: 사용자가 로그인되지 않은 상태에서도 실행됨 (관리자 권한으로 재등록 필요).
# Interactive는 사용자가 로그인되어 있어야만 17:00에 실행되어, 자리 비웠을 때 매번 스킵됨.
$principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType S4U -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "Scheduled task created:"
Write-Host $TaskName
