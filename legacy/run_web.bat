@echo off
REM ELS radar web server + cloudflare tunnel autostart (logon task).
REM Both run HIDDEN (no taskbar window to accidentally close).
cd /d "%~dp0"
if not exist "logs" mkdir "logs"

netstat -ano | findstr ":8000 " | findstr "LISTENING" >nul
if %ERRORLEVEL% NEQ 0 (
  powershell -NoProfile -WindowStyle Hidden -Command "Start-Process -WindowStyle Hidden -WorkingDirectory '%~dp0' -FilePath python -ArgumentList 'manage.py','runserver','0.0.0.0:8000','--noreload' -RedirectStandardOutput '%~dp0logs\web.log' -RedirectStandardError '%~dp0logs\web_err.log'"
)

tasklist | findstr /i "cloudflared" >nul
if %ERRORLEVEL% NEQ 0 (
  powershell -NoProfile -WindowStyle Hidden -Command "Start-Process -WindowStyle Hidden -WorkingDirectory '%~dp0' -FilePath 'C:\Program Files (x86)\cloudflared\cloudflared.exe' -ArgumentList '--logfile','%~dp0logs\tunnel.log','tunnel','run','els'"
)
