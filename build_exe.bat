@echo off
REM Build JarvisPanel.exe - run this on Windows with Python installed
cd /d "%~dp0"
echo Installing PyInstaller and dependencies...
pip install pyinstaller -r requirements.txt
if errorlevel 1 goto :error

echo.
echo Building JarvisPanel.exe (this takes 2-5 minutes)...
pyinstaller --clean --noconfirm jarvis_panel.spec
if errorlevel 1 goto :error

echo.
echo DONE! Your exe is at: dist\JarvisPanel.exe
echo You can move it anywhere and double-click to run.
pause
goto :eof

:error
echo BUILD FAILED - see errors above
pause
