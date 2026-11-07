# Written by install_server_startup.ps1 - waits for the server (up to 3 minutes), then opens the dashboard.
$url = 'http://localhost:8000/'
for ($i = 0; $i -lt 90; $i++) {
    try { Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2 | Out-Null; break } catch { Start-Sleep -Seconds 2 }
}
Start-Process $url
