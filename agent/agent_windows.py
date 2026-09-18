"""
Windows Device Tracking Agent
------------------------------
Collects hardware stats + connected/internal device info from this Windows PC
and POSTs it to your central server every REPORT_INTERVAL seconds.

Also detects clean shutdown/logoff (Ctrl+C, console close, Windows shutdown)
and notifies the server so it shows "offline" (not "crashed") on the dashboard.

Requirements (install with pip):
    pip install psutil requests wmi pywin32

Run:
    python agent_windows.py
"""

import platform
import socket
import time
import uuid
import json
import tomli as tomllib

import psutil
import requests
import wmi
import win32api
import win32con

with open("config.toml", "rb") as f:
    config = tomllib.load(f)

SERVER_URL = f'http://{config["SERVER_ADDRESS"]}:{config["PORT"]}/api/report'
SHUTDOWN_URL = f'http://{config["SERVER_ADDRESS"]}:{config["PORT"]}/api/shutdown'
API_KEY = config["API_KEY"]
DISPLAY_NAME = config["DISPLAY_NAME"]
REPORT_INTERVAL = config["REPORT_INTERVAL"]

# A stable ID for this machine (based on MAC address, stays the same across reboots)
DEVICE_ID = f"{socket.gethostname()}-{uuid.getnode():x}"


def get_hardware_stats():
    disk = psutil.disk_usage("C:\\")
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.5),
        "ram_percent": psutil.virtual_memory().percent,
        "ram_used_gb": round(psutil.virtual_memory().used / (1024 ** 3), 2),
        "ram_total_gb": round(psutil.virtual_memory().total / (1024 ** 3), 2),
        "disk_percent": disk.percent,
        "disk_free_gb": round(disk.free / (1024 ** 3), 2),
        "battery_percent": (psutil.sensors_battery().percent if psutil.sensors_battery() else None),
    }


def get_connected_devices():
    """List external/peripheral devices currently connected — for a quick presence check."""
    c = wmi.WMI()
    devices = []

    class_labels = {
        "USB": "USB Device",
        "DiskDrive": "Storage Drive",
        "Image": "Camera/Scanner",
        "Media": "Audio Device",
        "Mouse": "Mouse",
        "Keyboard": "Keyboard",
        "Monitor": "Monitor",
        "Net": "Network Adapter",
    }

    for entry in c.Win32_PnPEntity():
        if entry.Name and entry.Status == "OK" and entry.PNPClass in class_labels:
            devices.append({
                "type": class_labels[entry.PNPClass],
                "name": entry.Name,
            })

    return devices


def get_internal_components():
    """List internal hardware components — for verifying nothing's missing/swapped."""
    c = wmi.WMI()
    components = []

    for mem in c.Win32_PhysicalMemory():
        cap_gb = round(int(mem.Capacity) / (1024 ** 3)) if mem.Capacity else "?"
        components.append({
            "type": "RAM Stick",
            "name": f"{mem.Manufacturer or 'Unknown'} {cap_gb}GB",
            "slot": mem.DeviceLocator,
        })

    for cpu in c.Win32_Processor():
        components.append({"type": "CPU", "name": cpu.Name.strip()})

    for board in c.Win32_BaseBoard():
        components.append({"type": "Motherboard", "name": f"{board.Manufacturer} {board.Product}"})

    for gpu in c.Win32_VideoController():
        components.append({"type": "GPU", "name": gpu.Name})

    for disk in c.Win32_DiskDrive():
        components.append({
            "type": "Internal Drive" if disk.InterfaceType in ("IDE", "SCSI") else "External Drive",
            "name": disk.Model,
            "size_gb": round(int(disk.Size) / (1024 ** 3), 2) if disk.Size else None,
        })

    return components


def get_custom_data():
    return {"note": "replace get_custom_data() with your real app data"}


def build_payload():
    return {
        "device_id": DEVICE_ID,
        "hostname": DISPLAY_NAME,
        "os": f"{platform.system()} {platform.release()}",
        "timestamp": time.time(),
        "hardware": get_hardware_stats(),
        "devices": get_connected_devices(),
        "internal": get_internal_components(),
        "custom": get_custom_data(),
    }


def send_report(payload):
    try:
        resp = requests.post(
            SERVER_URL,
            headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=5,
        )
        if resp.status_code == 200:
            print(f"[OK] Reported at {time.strftime('%H:%M:%S')}")
        else:
            print(f"[WARN] Server responded {resp.status_code}: {resp.text}")
    except requests.RequestException as e:
        print(f"[ERROR] Could not reach server: {e}")


def send_shutdown_signal():
    """Best-effort notification to the server that this PC is shutting down cleanly."""
    try:
        requests.post(
            SHUTDOWN_URL,
            headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
            data=json.dumps({"device_id": DEVICE_ID}),
            timeout=3,
        )
        print("[INFO] Sent clean-shutdown signal to server.")
    except requests.RequestException as e:
        print(f"[WARN] Could not send shutdown signal: {e}")


def console_ctrl_handler(ctrl_type):
    """
    Fires on Ctrl+C, console window close, user logoff, or Windows shutdown.
    This is what lets us tell the server 'this was a clean shutdown' rather
    than just going silent (which the server would otherwise flag as a crash).
    """
    if ctrl_type in (
        win32con.CTRL_C_EVENT,
        win32con.CTRL_BREAK_EVENT,
        win32con.CTRL_CLOSE_EVENT,
        win32con.CTRL_LOGOFF_EVENT,
        win32con.CTRL_SHUTDOWN_EVENT,
    ):
        send_shutdown_signal()
        return False  # let default handling continue (allow the process to actually exit)
    return False


def main():
    win32api.SetConsoleCtrlHandler(console_ctrl_handler, True)

    print(f"Starting agent for device: {DEVICE_ID}")
    print(f"Reporting to: {SERVER_URL} every {REPORT_INTERVAL}s")
    try:
        while True:
            payload = build_payload()
            send_report(payload)
            time.sleep(REPORT_INTERVAL)
    except KeyboardInterrupt:
        send_shutdown_signal()


if __name__ == "__main__":
    main()