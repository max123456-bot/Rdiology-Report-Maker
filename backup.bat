@echo off
REM ===================================================================
REM  Pull everything back to this PC as plain JSON files.
REM
REM  Every doctor template, every learned word and correction, copied
REM  off whatever hosting you are using into a dated folder here.
REM
REM  Run this monthly. Free database tiers get paused, moved and in
REM  some cases deleted - this is the copy that does not depend on
REM  anyone else's terms of service.
REM ===================================================================

cd /d "%~dp0"

if "%STORAGE_URL%"=="" (
    echo.
    echo  STORAGE_URL is not set, so there is nothing remote to back up.
    echo.
    echo  Set it to the database you are using, then run this again:
    echo.
    echo      set STORAGE_URL=postgresql://user:pass@host/dbname
    echo      backup.bat
    echo.
    echo  The connection string is on your database provider's dashboard.
    echo.
    pause
    exit /b 1
)

for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value') do set DT=%%I
set STAMP=%DT:~0,4%-%DT:~4,2%-%DT:~6,2%
set DEST=backups\%STAMP%

echo.
echo  Backing up to %DEST%
echo.

if not exist "backups" mkdir "backups"
if not exist "%DEST%" mkdir "%DEST%"

python migrate_storage.py --from "%STORAGE_URL%" --to files --overwrite
if errorlevel 1 (
    echo.
    echo  Backup FAILED. Do not assume you have a copy.
    pause
    exit /b 1
)

xcopy /Y /I /Q "templates\*.json" "%DEST%\" >nul

echo.
echo ===================================================================
echo   Done. A copy is in %DEST%
echo.
echo   Keep one of these somewhere that is not this PC - a NAS, a
echo   USB drive, or cloud storage. A backup on the same machine
echo   does not survive that machine failing.
echo ===================================================================
echo.
pause
