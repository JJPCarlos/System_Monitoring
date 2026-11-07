# Device Tracker SERVER - why isn't it running? Run via check_server.bat (or from install_server_startup.ps1).
# Read-only: it changes nothing.
param([int]$Port = 0)
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$runBat = Join-Path $here 'run_server_startup.bat'
if (-not $Port) {
    $Port = 8000
    if (Test-Path $runBat) { $m = Select-String -Path $runBat -Pattern '--port (\d+)' | Select-Object -First 1; if ($m) { $Port = [int]$m.Matches[0].Groups[1].Value } }
}
function Ok($t)   { Write-Host "  OK    $t" -ForegroundColor Green }
function Bad($t)  { Write-Host "  PROBLEM  $t" -ForegroundColor Red }
function Info($t) { Write-Host "  $t" }

Write-Host ""
Write-Host "== Device Tracker server check (port $Port) =="

# 1. scheduled task
$task = Get-ScheduledTask -TaskName 'DeviceTrackerServer' -ErrorAction SilentlyContinue
if (-not $task) { Bad "The startup task 'DeviceTrackerServer' doesn't exist. Run install_server_startup.bat." }
else {
    $ti = Get-ScheduledTaskInfo -TaskName 'DeviceTrackerServer'
    $code = $ti.LastTaskResult
    $meaning = switch ($code) { 0 {'finished OK'} 267009 {'running now'} 267011 {'has never run yet'} 267014 {'was stopped'} 1 {'the server exited with an error (see server.log below)'} 2147942402 {'a file was not found (folder moved? run the installer again)'} default {'error'} }
    Info "Task state: $($task.State) - last run: $($ti.LastRunTime) - result: $code ($meaning)"
    Info "Starts on: $(($task.Triggers | ForEach-Object { $_.CimClass.CimClassName -replace 'MSFT_Task','' -replace 'Trigger','' }) -join ', ')"
    if ($task.State -eq 'Disabled') { Bad "The task is disabled. Enable it in Task Scheduler, or run install_server_startup.bat again." }
}

# 2. process + port + answer
$proc = Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -like '*uvicorn*server:app*' }
if ($proc) { Ok "Server process is running (process $(($proc | Select-Object -ExpandProperty ProcessId) -join ', '))" } else { Bad "No server process is running." }
$listen = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listen) {
    $owner = Get-Process -Id ($listen | Select-Object -First 1).OwningProcess -ErrorAction SilentlyContinue
    if ($proc -and ($proc.ProcessId -contains $owner.Id -or $proc.ParentProcessId -contains $owner.Id)) { Ok "Port $Port is open" }
    else { Info "Port $Port is held by '$($owner.ProcessName)' (process $($owner.Id)). If that isn't the Device Tracker server, it's blocking it - close it or use another port." }
} else { Bad "Nothing is listening on port $Port." }
try { Invoke-WebRequest -Uri "http://localhost:$Port/" -UseBasicParsing -TimeoutSec 3 | Out-Null; Ok "Dashboard answers at http://localhost:$Port/" }
catch { Bad "Dashboard does not answer at http://localhost:$Port/" }

# 3. common causes
$venvPy = Join-Path $here 'venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) { Bad "No venv in this folder. Run install_server_startup.bat." }
$cfg = Join-Path $here 'venv\pyvenv.cfg'
if ((Test-Path $cfg) -and ((Get-Content $cfg -Raw) -like '*WindowsApps*')) { Bad "The venv uses the Microsoft Store Python, which can't run at boot. Run install_server_startup.bat again (it rebuilds the venv)." }
if ($here -like '*\OneDrive*') { Bad "The server folder is inside OneDrive. The boot-time server may not be able to read it. Move it to e.g. C:\DeviceTracker and run the installer again from there." }
if ((Test-Path $runBat) -and -not ((Get-Content $runBat -Raw) -like "*$here*")) { Bad "run_server_startup.bat points at another folder (was the folder moved?). Run install_server_startup.bat again." }
$fast = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' -ErrorAction SilentlyContinue).HiberbootEnabled
if ($fast -eq 1) { Info "Note: Windows 'Fast startup' is on, so Shut down + power on isn't a full boot. The task also starts at log in to cover that." }

# 4. log
$log = Join-Path $here 'server.log'
Write-Host ""
if (Test-Path $log) {
    Write-Host "== Last 25 lines of server.log =="
    Get-Content $log -Tail 25
    if ((Get-Content $log -Tail 5) -match 'No module named') {
        Bad "The venv is missing the server's packages. Run install_server_startup.bat again (it installs them), or run:"
        Info "  `"$venvPy`" -m pip install -r `"$(Join-Path $here 'requirements.txt')`""
    }
} else { Info "No server.log yet - the task has never managed to start the server." }
Write-Host ""