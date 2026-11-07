@echo off
rem Removes what install_server_startup.bat set up (Windows). The database and server files stay.
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Asking for administrator permission...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
schtasks /end /tn DeviceTrackerServer >nul 2>&1
schtasks /delete /tn DeviceTrackerServer /f
schtasks /delete /tn DeviceTrackerDashboard /f
powershell -NoProfile -Command "Get-NetFirewallRule -DisplayName 'Device Tracker server' -ErrorAction SilentlyContinue | Remove-NetFirewallRule"
rem stop a server still running from the task (a venv's python.exe starts the real python as a child,
rem so match the command line rather than the file path)
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*uvicorn server:app*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
del "%~dp0run_server_startup.bat" >nul 2>&1
del "%~dp0open_dashboard.ps1" >nul 2>&1
echo.
echo Removed. The server no longer starts at boot. (server.log, the venv and the database were left in this folder.)
pause
