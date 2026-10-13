@echo off
rem ---------------------------------------------------------------
rem  Device Tracker agent - run at COMPUTER STARTUP (before anyone logs in)
rem  Right-click -> Run as administrator (it will also ask by itself).
rem  Needs install_startup.ps1 in the same folder.
rem ---------------------------------------------------------------
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Asking for administrator permission...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_startup.ps1"
echo.
pause
