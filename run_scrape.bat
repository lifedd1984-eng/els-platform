@echo off
REM ELS 플랫폼 — KOFIA 직접수집 배치 (작업 스케줄러가 호출)
REM 실행 로그를 logs\scrape_YYYYMMDD.log 에 append 한다.

cd /d "%~dp0"
if not exist "logs" mkdir "logs"

set "LOGFILE=logs\scrape_%date:~0,4%%date:~5,2%%date:~8,2%.log"

echo ================================================== >> "%LOGFILE%"
echo [%date% %time%] scrape_kofia 시작 >> "%LOGFILE%"

python "%~dp0manage.py" scrape_kofia >> "%LOGFILE%" 2>&1

echo [%date% %time%] scrape_kofia 종료 (exit=%ERRORLEVEL%) >> "%LOGFILE%"
