# Registers the Device Tracker agent as a scheduled task that starts when the PC boots
# (as the SYSTEM account, so it runs even at the login screen). Run via install_startup.bat.
$ErrorActionPreference = 'Stop'
$here     = Split-Path -Parent $MyInvocation.MyCommand.Path
$taskName = 'DeviceTrackerAgent'

try {
    if (-not (Test-Path (Join-Path $here 'agent_windows.py'))) { throw "agent_windows.py not found in $here" }
    if (-not (Test-Path (Join-Path $here 'config.toml')))      { throw "config.toml not found in $here - copy config.example.toml to config.toml and edit it first." }

    # --- find the real python.exe (not the Microsoft Store shortcut) ---
    $py = $null
    foreach ($cmd in @('python.exe', 'py.exe')) {
        if (Get-Command $cmd -ErrorAction SilentlyContinue) {
            $ErrorActionPreference = 'Continue'
            $pyArgs = if ($cmd -eq 'py.exe') { @('-3', '-c', 'import sys;print(sys.executable)') } else { @('-c', 'import sys;print(sys.executable)') }
            $out = & $cmd @pyArgs 2>$null
            $ErrorActionPreference = 'Stop'
            if ($LASTEXITCODE -eq 0 -and $out -and (Test-Path "$out")) { $py = "$out".Trim(); break }
        }
    }
    if (-not $py) { throw "Python was not found. Install Python for all users (python.org) and tick 'Add to PATH'." }
    if ($py -like '*\WindowsApps\*') { throw "Python from the Microsoft Store can't be used at startup ($py). Install it from python.org instead." }
    Write-Host "Python:  $py"

    # --- the SYSTEM account must be able to import everything the agent needs ---
    $ErrorActionPreference = 'Continue'
    $check = & $py -c "import psutil, requests, wmi, win32api, pystray, PIL; print(wmi.__file__)" 2>&1
    $code = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    if ($code -ne 0) { throw "Missing Python packages:`n$check`nRun:  `"$py`" -m pip install -r requirements.txt   (as administrator, without --user)" }
    if ("$check" -like '*\AppData\Roaming\*') {
        throw "The packages are installed only for your own user ($check), so the SYSTEM account can't see them.`nRe-install them for everyone, from an administrator prompt:`n  `"$py`" -m pip install -r requirements.txt"
    }

    # --- small launcher: the agent reads config.toml from the folder it starts in ---
    $wrapper = Join-Path $here 'run_agent_startup.bat'
    $lines = @(
        '@echo off',
        'rem Created by install_startup.ps1 - starts the agent from its own folder.',
        "cd /d `"$here`"",
        "`"$py`" agent_windows.py > `"$here\agent_startup.log`" 2>&1"
    )
    Set-Content -Path $wrapper -Value $lines -Encoding Default

    # --- the scheduled task ---
    $action    = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"`"$wrapper`"`"" -WorkingDirectory $here
    $trigger   = New-ScheduledTaskTrigger -AtStartup
    $trigger.Delay = 'PT30S'                       # give the network and Windows a moment to come up
    $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
                   -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
                   -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null

    # --- let the server ping the agent (PING_PORT in config.toml, default 8770; 0 = off) ---
    $pingPort = 8770
    $m = Select-String -Path (Join-Path $here 'config.toml') -Pattern '^\s*PING_PORT\s*=\s*(\d+)' | Select-Object -First 1
    if ($m) { $pingPort = [int]$m.Matches[0].Groups[1].Value }
    $ruleName = 'Device Tracker agent ping'
    Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    if ($pingPort -gt 0) {
        # Only computers on the same local network (the lab) may connect.
        New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP -LocalPort $pingPort `
            -RemoteAddress LocalSubnet -Action Allow -Profile Any | Out-Null
        Write-Host "Firewall: allowed the server to ping the agent on port $pingPort (local network only)."
    }

    Write-Host ""
    Write-Host "Done. The agent will start about 30 seconds after the PC boots, with or without anyone logged in." -ForegroundColor Green
    Write-Host "  Task name:  $taskName"
    Write-Host "  Log file:   $here\agent_startup.log   (reset at every boot)"
    Write-Host ""
    Write-Host "To test now:  schtasks /run /tn $taskName     (close any copy of the agent that is already running first)"
    Write-Host "Note: it runs without a tray icon, so 'Report a Problem' is not available from this PC."

    # --- warn about an old login-time shortcut that would start a second copy ---
    $startupDirs = @("$env:ProgramData\Microsoft\Windows\Start Menu\Programs\StartUp", "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\StartUp")
    $found = Get-ChildItem $startupDirs -ErrorAction SilentlyContinue | Where-Object { $_.Name -notlike 'desktop.ini' }
    if ($found) {
        Write-Host ""
        Write-Host "Check these Startup-folder items. If one of them starts the agent, delete it, or two copies will run:" -ForegroundColor Yellow
        $found | ForEach-Object { Write-Host "  $($_.FullName)" }
    }
}
catch {
    Write-Host ""
    Write-Host "Could not set up startup:" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}