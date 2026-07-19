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

REM 시세 갱신(낙인 경보) → 신규 상품 손실확률 순으로 이어서 실행
python "%~dp0manage.py" update_prices >> "%LOGFILE%" 2>&1
echo [%date% %time%] update_prices 종료 (exit=%ERRORLEVEL%) >> "%LOGFILE%"

python "%~dp0manage.py" simulate_products >> "%LOGFILE%" 2>&1
echo [%date% %time%] simulate_products 종료 (exit=%ERRORLEVEL%) >> "%LOGFILE%"
