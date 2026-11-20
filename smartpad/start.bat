@echo off
REM One-click start for Windows: creates the virtual environment on first run, then starts SmartPad.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv || (echo Python 3.10+ is required: https://www.python.org/downloads/ & pause & exit /b 1)
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || (echo Dependency install failed. & pause & exit /b 1)
)
if not exist ".env" copy ".env.example" ".env" >nul
".venv\Scripts\python.exe" run.py %*
pause
