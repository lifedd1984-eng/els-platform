@echo off
REM Pull latest ELS DB backup from EC2 to F:\ELS_backup (offsite copy).
REM EC2 keeps 7 local; this PC keeps 30. Runs daily 10:30 via Task Scheduler.
REM ASCII only in this file (CP949 breaks Korean comments in .bat).
REM
REM 2026-08-04: added timeouts. Without them a half-open TCP connection made
REM ssh wait forever - the task sat in "Running" for hours and the offsite
REM backup silently stopped for 3 days (Aug 2-4). BatchMode also stops any
REM prompt from blocking, since a scheduled task has no console to answer it.
REM
REM Note: "-o Key=Value" cannot sit inside a for /f backtick block (cmd splits
REM on '='), so ssh output goes to a temp file and we read that file instead.

set KEY=C:\Users\Taehoon\.ssh\taxdown
set REMOTE=ubuntu@54.180.166.91
set DEST=F:\ELS_backup
set LOG=%~dp0logs\pull_backup.log
set TMPF=%TEMP%\els_latest_backup.txt
set SSHOPT=-o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=4 -o StrictHostKeyChecking=accept-new

if not exist "%DEST%" mkdir "%DEST%"
if not exist "%~dp0logs" mkdir "%~dp0logs"

echo [%date% %time%] pull_backup start >> "%LOG%"

C:\Windows\System32\OpenSSH\ssh.exe %SSHOPT% -i %KEY% %REMOTE% "ls -t /home/ubuntu/els/backups/db_*.gz 2>/dev/null | head -1" > "%TMPF%" 2>> "%LOG%"
if errorlevel 1 (
  echo [%date% %time%] ssh FAILED - cannot list backups >> "%LOG%"
  exit /b 1
)

set LATEST=
for /f "usebackq delims=" %%f in ("%TMPF%") do set LATEST=%%f
del "%TMPF%" 2>nul

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
C:\Windows\System32\OpenSSH\scp.exe %SSHOPT% -i %KEY% %REMOTE%:"%LATEST%" "%DEST%\%BASENAME%" >> "%LOG%" 2>&1
if errorlevel 1 (
  echo [%date% %time%] scp FAILED >> "%LOG%"
  REM remove the partial file so a retry does not think it already has it
  del "%DEST%\%BASENAME%" 2>nul
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
