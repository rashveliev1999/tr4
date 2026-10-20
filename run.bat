@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.10+ is required. Install it from https://www.python.org/downloads/windows/
  pause
  exit /b 1
)
python -m pip install --upgrade -r requirements.txt
if errorlevel 1 goto fail
python trimui_companion.py
if errorlevel 1 goto fail
exit /b 0
:fail
echo.
echo Could not start the app. Read the error above.
pause
exit /b 1
