@echo off
rem Removes the Device Tracker startup task created by install_startup.bat.
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Asking for administrator permission...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
schtasks /end /tn DeviceTrackerAgent >nul 2>&1
schtasks /delete /tn DeviceTrackerAgent /f
del "%~dp0run_agent_startup.bat" >nul 2>&1
echo.
echo Startup task removed. (agent_startup.log, if present, was left in this folder.)
pause
