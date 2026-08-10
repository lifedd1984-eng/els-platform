# ELS 플랫폼 — KOFIA 직접수집(scrape_kofia)을 매주 월·수·금 10:00에 자동 실행
# 관리자 권한 PowerShell에서 실행: .\register_scheduler.ps1

$taskName = "ELS_Platform_Scrape"
$batPath  = Join-Path $PSScriptRoot "run_scrape.bat"

# run_scrape.bat 이 로그와 함께 python manage.py scrape_kofia 를 실행
$action = New-ScheduledTaskAction -Execute $batPath -WorkingDirectory $PSScriptRoot

# 매주 월·수·금 오전 10:00
$trigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday, Wednesday, Friday -At "10:00"

# PC가 그 시각에 꺼져 있었으면 켜진 직후 실행, 배터리에서도 실행
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopOnIdleEnd -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

# 기존 동일 작업 있으면 교체
try { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop } catch {}

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "KOFIA 청약중 ELS 자동수집 (월·수·금 10:00)"

Write-Host "등록 완료: 매주 월·수·금 10:00 scrape_kofia 실행 ($taskName)"
Write-Host "로그: $($PSScriptRoot)\logs\scrape_YYYYMMDD.log"
