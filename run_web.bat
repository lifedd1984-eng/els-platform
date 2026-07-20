@echo off
REM ELS platform web server + cloudflare tunnel autostart (logon task)
cd /d "%~dp0"
if not exist "logs" mkdir "logs"

netstat -ano | findstr ":8000 " | findstr "LISTENING" >nul
if %ERRORLEVEL% NEQ 0 (
  start "ELS_WEB" /min cmd /c "python manage.py runserver 0.0.0.0:8000 --noreload >> logs\web.log 2>&1"
)

tasklist | findstr /i "cloudflared" >nul
if %ERRORLEVEL% NEQ 0 (
  start "ELS_TUNNEL" /min cmd /c ""C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel run els >> logs\tunnel.log 2>&1"
)
