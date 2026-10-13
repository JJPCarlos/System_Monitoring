@echo off
rem Created by install_startup.ps1 - starts the agent from its own folder.
cd /d "C:\Users\Jester Carlos\Documents\Python Stuff\System_Monitoring\agent"
"C:\Users\Jester Carlos\AppData\Local\Programs\Python\Python314\python.exe" agent_windows.py > "C:\Users\Jester Carlos\Documents\Python Stuff\System_Monitoring\agent\agent_startup.log" 2>&1
