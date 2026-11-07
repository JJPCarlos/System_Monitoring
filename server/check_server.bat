@echo off
rem Device Tracker SERVER - shows whether the server is running and, if not, why. Changes nothing.
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Asking for administrator permission...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0check_server.ps1"
pause
