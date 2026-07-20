@echo off
REM ELS platform batch: scrape -> prices -> redemption check -> simulate -> digest
REM Sends a telegram summary (success/failure per step) at the end.
cd /d "%~dp0"
if not exist "logs" mkdir "logs"

set "LOGFILE=logs\scrape_%date:~0,4%%date:~5,2%%date:~8,2%.log"

echo ================================================== >> "%LOGFILE%"
echo [%date% %time%] scrape_kofia start >> "%LOGFILE%"
python "%~dp0manage.py" scrape_kofia >> "%LOGFILE%" 2>&1
set "EC_SCRAPE=%ERRORLEVEL%"
echo [%date% %time%] scrape_kofia end exit=%EC_SCRAPE% >> "%LOGFILE%"

python "%~dp0manage.py" update_prices >> "%LOGFILE%" 2>&1
set "EC_PRICES=%ERRORLEVEL%"
echo [%date% %time%] update_prices end exit=%EC_PRICES% >> "%LOGFILE%"

python "%~dp0manage.py" check_redemptions >> "%LOGFILE%" 2>&1
set "EC_REDEEM=%ERRORLEVEL%"
echo [%date% %time%] check_redemptions end exit=%EC_REDEEM% >> "%LOGFILE%"

python "%~dp0manage.py" simulate_products >> "%LOGFILE%" 2>&1
set "EC_SIM=%ERRORLEVEL%"
echo [%date% %time%] simulate_products end exit=%EC_SIM% >> "%LOGFILE%"

python "%~dp0manage.py" send_digest >> "%LOGFILE%" 2>&1
set "EC_DIGEST=%ERRORLEVEL%"
echo [%date% %time%] send_digest end exit=%EC_DIGEST% >> "%LOGFILE%"

python "%~dp0manage.py" notify_batch --results "scrape=%EC_SCRAPE%,prices=%EC_PRICES%,redeem=%EC_REDEEM%,simulate=%EC_SIM%,digest=%EC_DIGEST%" >> "%LOGFILE%" 2>&1
