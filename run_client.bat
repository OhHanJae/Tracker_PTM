@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python launcher 'py' was not found.
    echo Install Python 3.10 or later and enable the Python launcher.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Creating virtual environment...
    py -3 -m venv .venv
    if errorlevel 1 goto :fail
)

set "PY=.venv\Scripts\python.exe"

echo [2/3] Checking client dependencies...
"%PY%" -c "import PySide6, pygame" >nul 2>nul
if errorlevel 1 (
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 goto :fail
)

echo [3/3] Starting PT503 PySide6 API client...
"%PY%" main.py --client %*
exit /b %errorlevel%

:fail
echo.
echo [ERROR] Setup or launch failed.
pause
exit /b 1
