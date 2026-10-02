@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv
    if errorlevel 1 goto :fail
)

call ".venv\Scripts\activate.bat"
python -m pip install -r requirements-dev.txt
if errorlevel 1 goto :fail

pyinstaller --noconfirm --clean --onefile --windowed ^
  --name PT503_Tester ^
  --collect-all serial ^
  --collect-all pygame ^
  --add-data "pt503_tester/web;pt503_tester/web" ^
  main.py
if errorlevel 1 goto :fail

echo.
echo Build complete: dist\PT503_Tester.exe
pause
exit /b 0

:fail
echo.
echo [ERROR] Build failed.
pause
exit /b 1
