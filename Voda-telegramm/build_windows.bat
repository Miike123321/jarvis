@echo off
setlocal
cd /d "%~dp0"

py -3 -m pip install --upgrade pip
if errorlevel 1 exit /b %errorlevel%

py -3 -m pip install -r requirements.txt pyinstaller
if errorlevel 1 exit /b %errorlevel%

py -3 -m PyInstaller --noconfirm --clean --onefile --name aquabox_bot bot.py
if errorlevel 1 exit /b %errorlevel%

echo.
echo Build complete: dist\aquabox_bot.exe
echo Keep your .env file next to the executable.
pause