@echo off
REM ELS 플랫폼 웹서버 + Cloudflare 터널 자동시작 (로그온 시 작업 스케줄러가 호출)
cd /d "%~dp0"
if not exist "logs" mkdir "logs"

REM 이미 떠 있으면 중복 실행 방지
netstat -ano | findstr ":8000 " | findstr "LISTENING" >nul
if %ERRORLEVEL% NEQ 0 (
  start "ELS_WEB" /min cmd /c "python manage.py runserver 0.0.0.0:8000 --noreload >> logs\web.log 2>&1"
)

tasklist | findstr /i "cloudflared" >nul
if %ERRORLEVEL% NEQ 0 (
  start "ELS_TUNNEL" /min cmd /c ""C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel run els >> logs\tunnel.log 2>&1"
)
