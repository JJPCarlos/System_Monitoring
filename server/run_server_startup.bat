@echo off
rem Written by install_server_startup.ps1 - started by the DeviceTrackerServer task at boot.
cd /d "C:\Users\Jester Carlos\Documents\Python Stuff\System_Monitoring\server"
echo. >> server.log
echo ===== %date% %time% server starting ===== >> server.log
"C:\Users\Jester Carlos\Documents\Python Stuff\System_Monitoring\server\venv\Scripts\python.exe" -m uvicorn server:app --host 0.0.0.0 --port 8000 >> server.log 2>&1
