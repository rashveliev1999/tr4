@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.10+ is required. Install it from https://www.python.org/downloads/windows/
  pause
  exit /b 1
)
python -m pip install --upgrade -r requirements.txt pyinstaller
if errorlevel 1 goto fail
python -m PyInstaller --noconfirm --clean --onefile --windowed --name TrimUICompanionNX --icon TrimUICompanion.ico --add-data "TrimUICompanion.ico;." --collect-all customtkinter --collect-all PIL --collect-all paramiko --collect-all cryptography --collect-all bcrypt --collect-all nacl trimui_companion.py
if errorlevel 1 goto fail
echo.
echo Build complete: dist\TrimUICompanionNX.exe
pause
exit /b 0
:fail
echo.
echo Build failed. Check the error above.
pause
exit /b 1
