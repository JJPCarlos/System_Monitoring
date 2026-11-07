# Device Tracker SERVER - start at boot (Windows). Run via install_server_startup.bat (asks for admin).
#  1. Makes sure this folder has a Python venv with the server's requirements.
#  2. Task "DeviceTrackerServer": runs the server at COMPUTER STARTUP, hidden, as SYSTEM,
#     restarting it if it stops. Output goes to server.log in this folder.
#  3. Task "DeviceTrackerDashboard": when YOU log in, waits until the server answers,
#     then opens the dashboard in your default browser.
#  4. Windows Firewall: lets the lab PCs (local network only) reach the server port.
param(
    [int]$Port = 8000,
    [string]$User = "$env:USERDOMAIN\$env:USERNAME"   # who gets the dashboard opened at log in
)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

# --- 1. Python venv --------------------------------------------------------------------------
$venvPy = Join-Path $here 'venv\Scripts\python.exe'
# The server runs as SYSTEM at boot. SYSTEM can't start the Microsoft Store Python, and can't read
# files that OneDrive keeps "online only", so check both before setting anything up.
if ($here -like '*\OneDrive*') {
    Write-Host "WARNING: this folder is inside OneDrive ($here)." -ForegroundColor Yellow
    Write-Host "  The boot-time server may not be able to read it. Moving the server folder to e.g. C:\DeviceTracker is safer." -ForegroundColor Yellow
}
$cfg = Join-Path $here 'venv\pyvenv.cfg'
if ((Test-Path $cfg) -and ((Get-Content $cfg -Raw) -like '*WindowsApps*')) {
    Write-Host "The existing venv was made with the Microsoft Store Python, which can't run at boot. Rebuilding it..." -ForegroundColor Yellow
    Remove-Item (Join-Path $here 'venv') -Recurse -Force
}
if (-not (Test-Path $venvPy)) {
    Write-Host "Creating the Python environment (venv) - first time only..."
    # find the real python.exe (not the Microsoft Store shortcut)
    $basePy = $null
    $ErrorActionPreference = 'Continue'   # a missing launcher writes to stderr; don't let that stop the script
    foreach ($cmd in @('py.exe', 'python.exe')) {
        if (Get-Command $cmd -ErrorAction SilentlyContinue) {
            $pyArgs = if ($cmd -eq 'py.exe') { @('-3', '-c', 'import sys;print(sys.executable)') } else { @('-c', 'import sys;print(sys.executable)') }
            $out = (& $cmd @pyArgs 2>$null | Select-Object -First 1)
            if ($out -and (Test-Path "$out") -and ("$out" -notlike '*\WindowsApps\*')) { $basePy = "$out".Trim(); break }
        }
    }
    $ErrorActionPreference = 'Stop'
    if (-not $basePy) { throw "Python from python.org wasn't found (the Microsoft Store Python can't run at boot). Install Python 3 from python.org, tick 'Add to PATH', then run this again." }
    Write-Host "Python: $basePy"
    & $basePy -m venv venv
    & $venvPy -m pip install --upgrade pip | Out-Null
}
# Check the packages every time (a venv made by hand, or an install that was cut off, can be empty).
$ErrorActionPreference = 'Continue'
& $venvPy -c "import fastapi, uvicorn" 2>$null
$have = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = 'Stop'
if (-not $have) {
    Write-Host "Installing the server's Python packages (fastapi, uvicorn) into the venv..."
    $ErrorActionPreference = 'Continue'
    & $venvPy -m pip install -r (Join-Path $here 'requirements.txt')
    $pipCode = $LASTEXITCODE
    & $venvPy -c "import fastapi, uvicorn" 2>$null
    $have = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = 'Stop'
    if ($pipCode -ne 0 -or -not $have) { throw "pip couldn't install the requirements (check the internet connection), then run this again." }
}
Write-Host "Python packages: OK"

# --- stop any copy already running, so the new server.py (and its API key) is the one that runs ---
# (the task itself, plus a server started by hand: a venv's python.exe starts the real python as a
# child, so match the command line rather than the file path)
Get-ScheduledTask -TaskName DeviceTrackerServer -ErrorAction SilentlyContinue | Stop-ScheduledTask -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -like '*uvicorn*server:app*' } |
    ForEach-Object { Write-Host "Stopping the server that was already running (process $($_.ProcessId))"; Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1

# --- 2. Server task at startup ----------------------------------------------------------------
$runBat = Join-Path $here 'run_server_startup.bat'
@"
@echo off
rem Written by install_server_startup.ps1 - started by the DeviceTrackerServer task at boot.
cd /d "$here"
echo. >> server.log
echo ===== %date% %time% server starting ===== >> server.log
"$venvPy" -m uvicorn server:app --host 0.0.0.0 --port $Port >> server.log 2>&1
"@ | Set-Content -Path $runBat -Encoding Default   # the PC's own code page, so folder names with letters like n-tilde still work

$action    = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$runBat`"" -WorkingDirectory $here
# Boot AND any log in: with Windows "Fast startup" on, Shut down + power on is not a real boot, so the
# boot trigger alone may not fire. If the server is already running, the extra start is ignored.
$trigger   = @((New-ScheduledTaskTrigger -AtStartup), (New-ScheduledTaskTrigger -AtLogOn))
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
               -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName 'DeviceTrackerServer' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "Server: will start at boot and at log in (task 'DeviceTrackerServer'). Log: $here\server.log"

# --- 3. Open the dashboard when you log in -----------------------------------------------------
$openPs1 = Join-Path $here 'open_dashboard.ps1'
@"
# Written by install_server_startup.ps1 - waits for the server (up to 3 minutes), then opens the dashboard.
`$url = 'http://localhost:$Port/'
for (`$i = 0; `$i -lt 90; `$i++) {
    try { Invoke-WebRequest -Uri `$url -UseBasicParsing -TimeoutSec 2 | Out-Null; break } catch { Start-Sleep -Seconds 2 }
}
Start-Process `$url
"@ | Set-Content -Path $openPs1 -Encoding UTF8

$openAction  = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$openPs1`""
$openTrigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$openPrin    = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'DeviceTrackerDashboard' -Action $openAction -Trigger $openTrigger -Principal $openPrin `
    -Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries) -Force | Out-Null
Write-Host "Dashboard: opens in the default browser when $User logs in (task 'DeviceTrackerDashboard')."

# --- 4. Firewall -----------------------------------------------------------------------------
$ruleName = 'Device Tracker server'
Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP -LocalPort $Port `
    -RemoteAddress LocalSubnet -Action Allow -Profile Any | Out-Null
Write-Host "Firewall: lab PCs on the local network can reach port $Port."

# --- start it now, no reboot needed, and check that it really answers ---------------------------
Start-ScheduledTask -TaskName 'DeviceTrackerServer'
Write-Host ""
Write-Host "Starting the server and checking it answers (up to 40 seconds)..."
$up = $false
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 2
    try { Invoke-WebRequest -Uri "http://localhost:$Port/" -UseBasicParsing -TimeoutSec 2 | Out-Null; $up = $true; break } catch { }
}
if ($up) {
    Write-Host "Started. The dashboard is at http://localhost:$Port/ (other PCs: http://<this PC's IP>:$Port/)" -ForegroundColor Green
    Start-Process powershell.exe -ArgumentList "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$openPs1`""
} else {
    Write-Host "The server did NOT start. Details:" -ForegroundColor Red
    & (Join-Path $here 'check_server.ps1') -Port $Port
}