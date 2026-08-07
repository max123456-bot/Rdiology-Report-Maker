@echo off
REM ===================================================================
REM  Run the report generator for the whole clinic.
REM
REM  This PC becomes the server. Every other PC opens it in a browser.
REM  Nothing leaves the building: the reports, the templates and
REM  everything the app has learned all stay on this machine.
REM
REM  Leave this window open. Closing it stops the server for everyone.
REM ===================================================================

cd /d "%~dp0"
title Radiology Report Generator - clinic server (leave this window open)

REM Several people editing at once needs a transactional store, not loose files.
if not defined STORAGE_URL set "STORAGE_URL=sqlite:///data/reports.db"

python -c "import streamlit" 2>nul
if errorlevel 1 (
    echo Installing dependencies, one moment...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Could not install dependencies. Is Python installed and on PATH?
        pause
        exit /b 1
    )
)

if not exist "data\reports.db" (
    echo.
    echo First run - moving existing templates into the shared database...
    python migrate_storage.py --to "%STORAGE_URL%"
)

echo.
echo ===================================================================
echo   Other PCs in the clinic should open ONE of these:
echo.
python show_address.py
echo.
echo   This PC: http://localhost:8501
echo ===================================================================
echo.
echo   Leave this window open. Close it and the app stops for everyone.
echo.

python -m streamlit run app.py ^
    --server.address 0.0.0.0 ^
    --server.port 8501 ^
    --server.headless true ^
    --browser.gatherUsageStats false

echo.
echo The server has stopped.
pause
