# ELS 플랫폼 — 매일 09:00 import_els 배치를 Windows 작업 스케줄러에 등록
# 관리자 권한 PowerShell에서 실행: .\register_scheduler.ps1

$taskName = "ELS_Platform_Import"
$python = (Get-Command python).Source
$managePy = Join-Path $PSScriptRoot "manage.py"

$action = New-ScheduledTaskAction -Execute $python `
    -Argument "`"$managePy`" import_els" `
    -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Daily -At "09:00"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd

try { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop } catch {}
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings

Write-Host "등록 완료: 매일 09:00에 import_els 실행 ($taskName)"
