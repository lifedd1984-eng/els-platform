@echo off
REM Pull latest ELS DB backup from EC2 to F:\ELS_backup (offsite copy).
REM EC2 keeps 7 local; this PC keeps 30. Runs daily 10:30 via Task Scheduler.
REM ASCII only in this file (CP949 breaks Korean comments in .bat).

set KEY=C:\Users\Taehoon\.ssh\taxdown
set REMOTE=ubuntu@54.180.166.91
set DEST=F:\ELS_backup
set LOG=%~dp0logs\pull_backup.log

if not exist "%DEST%" mkdir "%DEST%"
if not exist "%~dp0logs" mkdir "%~dp0logs"

echo [%date% %time%] pull_backup start >> "%LOG%"

REM NOTE: no "-o Key=Value" options inside for /f (cmd splits on '=')
for /f "usebackq delims=" %%f in (`C:\Windows\System32\OpenSSH\ssh.exe -i %KEY% %REMOTE% "ls -t /home/ubuntu/els/backups/db_*.gz 2>/dev/null | head -1"`) do set LATEST=%%f

if "%LATEST%"=="" (
  echo [%date% %time%] no backup found on EC2 >> "%LOG%"
  exit /b 1
)

for %%b in ("%LATEST%") do set BASENAME=%%~nxb
if exist "%DEST%\%BASENAME%" (
  echo [%date% %time%] already have %BASENAME% - skip >> "%LOG%"
  exit /b 0
)

REM trailing backslash before closing quote breaks Windows arg parsing -> name the file
C:\Windows\System32\OpenSSH\scp.exe -i %KEY% %REMOTE%:"%LATEST%" "%DEST%\%BASENAME%" >> "%LOG%" 2>&1
if errorlevel 1 (
  echo [%date% %time%] scp FAILED >> "%LOG%"
  exit /b 1
)
echo [%date% %time%] pulled %BASENAME% >> "%LOG%"

REM keep newest 30 db_*.gz in DEST
set /a CNT=0
for /f "delims=" %%f in ('dir /b /o-d "%DEST%\db_*.gz" 2^>nul') do (
  set /a CNT+=1
  setlocal enabledelayedexpansion
  if !CNT! gtr 30 del "%DEST%\%%f"
  endlocal
)
echo [%date% %time%] pull_backup done >> "%LOG%"
