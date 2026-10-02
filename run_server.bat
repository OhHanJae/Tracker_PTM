@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "BOOTSTRAP_PYTHON="
set "BOOTSTRAP_ARG="
set "VENV_PYTHON=.venv-server\Scripts\python.exe"
set "VENV_OK="

if exist "%VENV_PYTHON%" (
    "%VENV_PYTHON%" -c "import sys" >nul 2>nul
    if not errorlevel 1 set "VENV_OK=1"
)

if not defined VENV_OK (
    if defined PYTHON call :try_python "%PYTHON%"
    if not defined BOOTSTRAP_PYTHON call :try_python "py" "-3"
    if not defined BOOTSTRAP_PYTHON call :try_python "python"

    if not defined BOOTSTRAP_PYTHON (
        echo [ERROR] Python 3 was not found. Set PYTHON to a valid python.exe path.
        exit /b 1
    )

    echo [1/3] Creating virtual environment...
    "!BOOTSTRAP_PYTHON!" !BOOTSTRAP_ARG! -m venv .venv-server
    if errorlevel 1 goto :fail
)

echo [2/3] Checking server dependencies...
"%VENV_PYTHON%" -c "import serial" >nul 2>nul
if errorlevel 1 (
    "%VENV_PYTHON%" -m pip install -r requirements-server.txt
    if errorlevel 1 goto :fail
)

echo [3/3] Starting PT503 headless Web/TCP server...
"%VENV_PYTHON%" main.py --server --host 0.0.0.0 --tcp-host 0.0.0.0 %*
exit /b %errorlevel%

:fail
echo.
echo [ERROR] Setup or launch failed.
exit /b 1

:try_python
set "CANDIDATE=%~1"
set "CANDIDATE_ARG=%~2"
if "%CANDIDATE_ARG%"=="" (
    "%CANDIDATE%" -c "import sys" >nul 2>nul
) else (
    "%CANDIDATE%" "%CANDIDATE_ARG%" -c "import sys" >nul 2>nul
)
if not errorlevel 1 (
    set "BOOTSTRAP_PYTHON=%CANDIDATE%"
    set "BOOTSTRAP_ARG=%CANDIDATE_ARG%"
)
exit /b 0
