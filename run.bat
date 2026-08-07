@echo off
REM Launch the HC Format Radiology Report Generator.
cd /d "%~dp0"

python -c "import streamlit" 2>nul
if errorlevel 1 (
    echo Installing dependencies, one moment...
    python -m pip install -r requirements.txt
)

echo.
echo Starting the app... your browser will open at http://localhost:8501
echo Close this window to stop it.
echo.
python -m streamlit run app.py
pause
