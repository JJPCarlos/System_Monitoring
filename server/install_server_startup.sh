#!/usr/bin/env bash
# Device Tracker SERVER - start at boot and open the dashboard at log in (Linux / Nobara).
# Run from anywhere as your normal user (NOT with sudo):   bash install_server_startup.sh [port]
#  1. Makes sure this folder has a Python venv with the server's requirements.
#  2. A systemd *user* service "device-tracker" runs the server and restarts it if it stops.
#     "linger" is turned on so it starts at boot even before anyone logs in. (A user service is
#     used instead of a system one because SELinux on Fedora/Nobara blocks system services from
#     running Python out of a home folder.)
#  3. An autostart entry opens the dashboard in your default browser when you log in to the desktop.
#  4. Opens the port in firewalld (if it's running) so the lab PCs can reach the server.
set -euo pipefail
PORT="${1:-8000}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ $EUID -eq 0 ]]; then echo "Run this as your normal user, not with sudo (it asks for sudo itself when needed)."; exit 1; fi
if ! [[ "$PORT" =~ ^[0-9]+$ ]]; then echo "Port must be a number, e.g. bash $0 8000"; exit 1; fi

# --- 1. Python venv ------------------------------------------------------------------------------
if [[ ! -x "$HERE/venv/bin/python" ]]; then
    echo "Creating the Python environment (venv) - first time only..."
    python3 -m venv "$HERE/venv"
    "$HERE/venv/bin/python" -m pip install --upgrade pip >/dev/null
    "$HERE/venv/bin/python" -m pip install -r "$HERE/requirements.txt"
fi

# --- 2. systemd user service ---------------------------------------------------------------------
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/device-tracker.service" <<EOF
[Unit]
Description=Device Tracker server (port $PORT)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$HERE
ExecStart="$HERE/venv/bin/python" -m uvicorn server:app --host 0.0.0.0 --port $PORT
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable device-tracker.service >/dev/null
systemctl --user restart device-tracker.service
echo "Server: runs as the user service 'device-tracker' (logs: journalctl --user -u device-tracker -f)"
if [[ "$(loginctl show-user "$USER" --property=Linger --value 2>/dev/null)" != "yes" ]]; then
    echo "Turning on 'linger' so the server starts at boot without logging in (asks for your password)..."
    sudo loginctl enable-linger "$USER"
fi

# --- 3. open the dashboard at log in -------------------------------------------------------------
cat > "$HERE/open_dashboard.sh" <<EOF
#!/usr/bin/env bash
# Written by install_server_startup.sh - waits for the server (up to 3 minutes), then opens the dashboard.
URL="http://localhost:$PORT/"
for _ in \$(seq 90); do
    if command -v curl >/dev/null; then curl -fs -o /dev/null --max-time 2 "\$URL" && break
    else python3 -c "import urllib.request,sys; urllib.request.urlopen(sys.argv[1], timeout=2)" "\$URL" 2>/dev/null && break; fi
    sleep 2
done
xdg-open "\$URL" >/dev/null 2>&1 &
EOF
chmod +x "$HERE/open_dashboard.sh"
mkdir -p "$HOME/.config/autostart"
cat > "$HOME/.config/autostart/device-tracker-dashboard.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Device Tracker dashboard
Comment=Opens the Device Tracker dashboard when you log in
Exec="$HERE/open_dashboard.sh"
Terminal=false
X-GNOME-Autostart-enabled=true
EOF
echo "Dashboard: opens in your default browser when you log in."

# --- 4. firewall ---------------------------------------------------------------------------------
if command -v firewall-cmd >/dev/null && sudo firewall-cmd --state >/dev/null 2>&1; then
    sudo firewall-cmd --permanent --add-port="$PORT/tcp" >/dev/null && sudo firewall-cmd --reload >/dev/null
    echo "Firewall: opened port $PORT/tcp."
fi

echo
sleep 2
if systemctl --user is-active --quiet device-tracker.service; then
    echo "Started. Dashboard: http://localhost:$PORT/  (other PCs: http://<this PC's IP>:$PORT/)"
    "$HERE/open_dashboard.sh"
else
    echo "The service didn't stay up - see: journalctl --user -u device-tracker -n 50"
    exit 1
fi
