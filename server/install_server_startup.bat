@echo off
rem ---------------------------------------------------------------
rem  Device Tracker SERVER - start at boot and open the dashboard at log in (Windows)
rem  Double-click it (it asks for administrator permission by itself).
rem  Needs install_server_startup.ps1 and requirements.txt in this folder.
rem  Optional: install_server_startup.bat -Port 8080   (default 8000)
rem ---------------------------------------------------------------
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Asking for administrator permission...
    if "%~1"=="" (
        powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    ) else (
        powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs"
    )
    exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_server_startup.ps1" %*
echo.
pause
