"""
Windows Device Tracking Agent (Tray Version)
----------------------------------------------
Runs quietly in the system tray. Collects hardware stats + connected/internal
device info from this Windows PC and POSTs it to your central server every
REPORT_INTERVAL seconds. Also lets anyone report a problem with ANY PC in the
room (not just this one) via a small form from the tray menu.

All settings live in config.toml (same folder as this script) — nothing to
edit in the code itself when deploying to a new PC.

Requirements (install with pip):
    pip install psutil requests wmi pywin32 pystray pillow tomli

Run:
    python agent_windows.py
(tkinter and the console window are hidden automatically once running)
"""

import ctypes
from ctypes import wintypes
import json
import os
import platform
import socket
import sys
import threading
import time
import tkinter as tk
import uuid
import webbrowser
from tkinter import messagebox

import psutil
import pystray
import pythoncom
import requests
import win32api
import win32con
import wmi
from PIL import Image, ImageDraw

# tomllib is built into Python 3.11+; older versions need the "tomli" backport package
if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

# ----------------- LOGGING (this app hides its console window, so anything
# printed with a normal print() would otherwise be completely invisible —
# redirect it to a log file next to the script instead) -----------------
_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent.log")
try:
    _log_file = open(_LOG_PATH, "a", encoding="utf-8", buffering=1)
    sys.stdout = _log_file
    sys.stderr = _log_file
except Exception:
    pass  # if logging setup itself fails, just continue without it rather than crash

print(f"\n{'='*60}\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] Agent starting up\n{'='*60}")

# ----------------- CONFIG (loaded from config.toml) -----------------
try:
    with open("config.toml", "rb") as f:
        config = tomllib.load(f)
except FileNotFoundError:
    print("[FATAL] config.toml not found. Copy config.example.toml to config.toml and edit it.")
    sys.exit(1)

SERVER_BASE = f'http://{config["SERVER_ADDRESS"]}:{config["PORT"]}'
API_KEY = config["API_KEY"]
DISPLAY_NAME = config["DISPLAY_NAME"]
REPORT_INTERVAL = config.get("REPORT_INTERVAL", 5)
DEVICE_WATCH_INTERVAL = config.get("DEVICE_WATCH_INTERVAL", 2)  # how often to check for plug/unplug, in seconds
ROOM = config.get("ROOM", "Unassigned")
SYSTEM_UNIT = config.get("SYSTEM_UNIT", "Unassigned")
# Pin the Keyboard/Mouse presence check to ONE specific device's VID/PID
# (e.g. "VID_0C45&PID_8006"), rather than "any device Windows classifies as
# Keyboard/Mouse". Leave blank to fall back to the old any-device behavior.
# Find your device's VID/PID by running diagnose_devices.py.
KEYBOARD_ID = config.get("KEYBOARD_ID", "").upper()
MOUSE_ID = config.get("MOUSE_ID", "").upper()
# -------------------------------------------

SERVER_URL = f"{SERVER_BASE}/api/report"
SHUTDOWN_URL = f"{SERVER_BASE}/api/shutdown"
ISSUE_URL = f"{SERVER_BASE}/api/report-issue"

DEVICE_ID = f"{socket.gethostname()}-{uuid.getnode():x}"
tray_icon = None  # set in main()


# ----------------- "IS THIS DEVICE ACTUALLY REMOVABLE?" (via SetupAPI) -----------------
# This uses the exact same CM_DEVCAP_REMOVABLE flag Windows itself uses to decide
# whether a device shows up under "Safely Remove Hardware". Built-in components
# (laptop keyboard, Precision touchpad) are marked NOT removable by their driver;
# anything genuinely external (wired USB, wireless dongle, Bluetooth) IS marked
# removable. This is far more reliable than guessing from the device ID prefix,
# which can't tell a wireless mouse's HID interface apart from a built-in
# touchpad's HID interface — both show up the same way there.
_setupapi = ctypes.WinDLL("setupapi")

_DIGCF_PRESENT = 0x02
_DIGCF_ALLCLASSES = 0x04
_SPDRP_CAPABILITIES = 0x0F
_CM_DEVCAP_REMOVABLE = 0x00000004
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("ClassGuid", ctypes.c_byte * 16),
        ("DevInst", wintypes.DWORD),
        ("Reserved", ctypes.c_void_p),
    ]


# Explicit argtypes/restype are required here — without them, ctypes guesses
# argument marshaling from the Python values passed in, which silently
# corrupts pointer-sized handles (HDEVINFO, buffer pointers) on 64-bit
# Windows. That bug was present before and made every removable-flag lookup
# silently fail, always falling back to the old prefix heuristic instead.
_setupapi.SetupDiGetClassDevsW.argtypes = [
    ctypes.c_void_p, wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD,
]
_setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p

_setupapi.SetupDiEnumDeviceInfo.argtypes = [
    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_SP_DEVINFO_DATA),
]
_setupapi.SetupDiEnumDeviceInfo.restype = wintypes.BOOL

_setupapi.SetupDiGetDeviceInstanceIdW.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(_SP_DEVINFO_DATA),
    wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
]
_setupapi.SetupDiGetDeviceInstanceIdW.restype = wintypes.BOOL

_setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(_SP_DEVINFO_DATA), wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
_setupapi.SetupDiGetDeviceRegistryPropertyW.restype = wintypes.BOOL

_setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
_setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL


def get_removable_flags() -> dict:
    """Returns {device_instance_id (upper-case): is_removable_bool} for every
    currently present PnP device. Built to fail soft — if anything about this
    goes wrong, returns {} and callers fall back to including the device."""
    flags = {}
    try:
        h_devinfo = _setupapi.SetupDiGetClassDevsW(
            None, None, None, _DIGCF_PRESENT | _DIGCF_ALLCLASSES
        )
        if not h_devinfo or h_devinfo == _INVALID_HANDLE_VALUE:
            return flags

        dev_info_data = _SP_DEVINFO_DATA()
        dev_info_data.cbSize = ctypes.sizeof(_SP_DEVINFO_DATA)
        index = 0
        while _setupapi.SetupDiEnumDeviceInfo(h_devinfo, index, ctypes.byref(dev_info_data)):
            index += 1
            buf = ctypes.create_unicode_buffer(512)
            required = wintypes.DWORD()
            if not _setupapi.SetupDiGetDeviceInstanceIdW(
                h_devinfo, ctypes.byref(dev_info_data), buf, 512, ctypes.byref(required)
            ):
                continue

            caps = wintypes.DWORD()
            reg_type = wintypes.DWORD()
            ok = _setupapi.SetupDiGetDeviceRegistryPropertyW(
                h_devinfo, ctypes.byref(dev_info_data), _SPDRP_CAPABILITIES,
                ctypes.byref(reg_type), ctypes.byref(caps), ctypes.sizeof(caps), None,
            )
            if ok:
                flags[buf.value.upper()] = bool(caps.value & _CM_DEVCAP_REMOVABLE)

        _setupapi.SetupDiDestroyDeviceInfoList(h_devinfo)
    except Exception as e:
        print(f"[WARN] Removable-flag lookup failed, falling back to include-all: {e}")
    return flags


# ----------------- PARENT-DEVICE WALK (for composite HID devices) -----------------
# Confirmed via real hardware testing: Windows often does NOT set the removable
# capability on a composite device's individual child interface (e.g. the
# "keyboard part" of a wireless dongle shows up as its own HID\... device with
# removable=False) — only on the actual physical USB device a couple of levels
# up the tree. This walks up from the given device toward the root using
# CM_Get_Parent, checking each ancestor's ID against the SAME removable_flags
# dict from get_removable_flags() above, and returns True the moment any
# ancestor is genuinely removable. Built-in devices (laptop keyboard, Precision
# touchpad) walk all the way to the true root and correctly stay False.
_cfgmgr32 = ctypes.WinDLL("cfgmgr32", use_last_error=True)

_cfgmgr32.CM_Locate_DevNodeW.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.LPCWSTR, wintypes.ULONG]
_cfgmgr32.CM_Locate_DevNodeW.restype = wintypes.DWORD

_cfgmgr32.CM_Get_Parent.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, wintypes.ULONG]
_cfgmgr32.CM_Get_Parent.restype = wintypes.DWORD

_cfgmgr32.CM_Get_Device_IDW.argtypes = [wintypes.DWORD, wintypes.LPWSTR, wintypes.ULONG, wintypes.ULONG]
_cfgmgr32.CM_Get_Device_IDW.restype = wintypes.DWORD


def is_removable_via_parent_walk(device_id: str, removable_flags: dict, max_depth: int = 6):
    """Returns (is_removable, physical_parent_id):
    - is_removable: True/False, or None if the device couldn't be located at all
      (caller should fall back to the prefix heuristic in that case).
    - physical_parent_id: the device ID of the ancestor that was found to be
      removable (i.e. the actual physical USB device), or None if not
      removable/not found. Devices sharing the same physical_parent_id are
      different logical interfaces of the same physical hardware — used to
      detect e.g. a mouse's macro-key buttons showing up as a separate
      "Keyboard" interface of that same physical mouse."""
    try:
        devinst = wintypes.DWORD()
        ret = _cfgmgr32.CM_Locate_DevNodeW(ctypes.byref(devinst), device_id, 0)
        if ret != 0:
            return None, None

        current = devinst.value
        for _ in range(max_depth):
            buf = ctypes.create_unicode_buffer(512)
            id_ret = _cfgmgr32.CM_Get_Device_IDW(current, buf, 512, 0)
            current_id = buf.value.upper() if id_ret == 0 else None
            if current_id and removable_flags.get(current_id) is True:
                return True, current_id

            parent = wintypes.DWORD()
            ret = _cfgmgr32.CM_Get_Parent(ctypes.byref(parent), current, 0)
            if ret != 0:
                break  # reached the true root
            current = parent.value

        return False, None
    except Exception as e:
        print(f"[WARN] Parent-walk removable check failed for {device_id}: {e}")
        return None, None


# ----------------- DATA COLLECTION -----------------
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


def get_connected_devices(wmi_conn=None):
    """List external/peripheral devices currently connected — for a quick presence check.
    Keyboard/Mouse are filtered by walking up the device tree (via CM_Get_Parent)
    to find whether the device — or any of its ancestors — is genuinely
    removable, using Windows' own CM_DEVCAP_REMOVABLE flag. This is necessary
    because Windows frequently does NOT set that flag on a composite device's
    individual child interface (e.g. the "keyboard part" of a wireless dongle),
    only on the real physical USB device 1-2 levels up — confirmed via direct
    testing on real hardware. Built-in devices (laptop keyboard, Precision
    touchpad) walk all the way to the true root and correctly stay excluded.

    NOTE: an earlier version also tried excluding a mouse's macro-key
    "Keyboard" interface by grouping devices under a shared physical parent.
    That was reverted — it can misfire when unrelated devices share an
    upstream USB hub (the walk can land on the hub itself rather than each
    device's own connection point), which caused a real external keyboard to
    wrongly disappear. A lone macro-key mouse showing a spurious Keyboard
    checkmark is an accepted cosmetic trade-off in exchange for never losing
    a real device."""
    c = wmi_conn or wmi.WMI()
    devices = []
    class_labels = {
        "USB": "USB Device", "DiskDrive": "Storage Drive", "Image": "Camera/Scanner",
        "Media": "Audio Device", "Mouse": "Mouse", "Keyboard": "Keyboard",
        "Monitor": "Monitor", "Net": "Network Adapter",
    }
    usb_only_classes = {"Mouse", "Keyboard"}
    removable_flags = get_removable_flags()

    for entry in c.Win32_PnPEntity():
        if not (entry.Name and entry.Status == "OK" and entry.PNPClass in class_labels):
            continue
        raw_device_id = entry.DeviceID or ""  # original case — matches what the diagnostic script used
        device_id = raw_device_id.upper()      # uppercase — used for the fallback prefix check

        if entry.PNPClass in usb_only_classes:
            is_removable, _parent_id = is_removable_via_parent_walk(raw_device_id, removable_flags)
            print(f"[DEBUG] {entry.PNPClass} '{entry.Name}' ({raw_device_id}) -> removable={is_removable}")
            if is_removable is False:
                continue  # Windows itself says this — and all its ancestors — are fixed, built-in
            if is_removable is None:
                # Couldn't determine — fall back to the old prefix heuristic
                # rather than silently dropping a real external device
                if not device_id.startswith(("USB", "HID", "BTHENUM", "BTHLE")):
                    continue

        devices.append({"type": class_labels[entry.PNPClass], "name": entry.Name, "device_id": device_id})

    return devices


def get_internal_components(wmi_conn=None):
    c = wmi_conn or wmi.WMI()
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


def compute_peripheral_flags(devices: list) -> dict:
    """Whether a keyboard/mouse is currently connected. If KEYBOARD_ID/MOUSE_ID
    are configured, this checks for that SPECIFIC device's VID/PID rather than
    any device Windows happens to classify as Keyboard/Mouse — sidesteps the
    macro-key-interface and combo-dongle ambiguity entirely, since it's keyed
    to one exact piece of hardware instead of a PnP class."""
    if KEYBOARD_ID:
        has_keyboard = any(
            d["type"] == "Keyboard" and KEYBOARD_ID in d.get("device_id", "") for d in devices
        )
    else:
        has_keyboard = any(d["type"] == "Keyboard" for d in devices)

    if MOUSE_ID:
        has_mouse = any(
            d["type"] == "Mouse" and MOUSE_ID in d.get("device_id", "") for d in devices
        )
    else:
        has_mouse = any(d["type"] == "Mouse" for d in devices)

    return {"has_keyboard": has_keyboard, "has_mouse": has_mouse}


def build_payload(wmi_conn=None):
    devices = get_connected_devices(wmi_conn)
    return {
        "device_id": DEVICE_ID,
        "hostname": DISPLAY_NAME,
        "room": ROOM,
        "system_unit": SYSTEM_UNIT,
        "os": f"{platform.system()} {platform.release()}",
        "timestamp": time.time(),
        "hardware": get_hardware_stats(),
        "devices": devices,
        "internal": get_internal_components(wmi_conn),
        "peripheral_flags": compute_peripheral_flags(devices),
        "custom": get_custom_data(),
    }


# ----------------- NETWORKING -----------------
def send_report(payload):
    try:
        resp = requests.post(
            SERVER_URL,
            headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=5,
        )
        if resp.status_code != 200:
            print(f"[WARN] Server responded {resp.status_code}: {resp.text}")
    except requests.RequestException as e:
        print(f"[ERROR] Could not reach server: {e}")


def send_shutdown_signal():
    try:
        requests.post(
            SHUTDOWN_URL,
            headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
            data=json.dumps({"device_id": DEVICE_ID}),
            timeout=3,
        )
    except requests.RequestException as e:
        print(f"[WARN] Could not send shutdown signal: {e}")


def send_issue_report(room, system_unit, description):
    try:
        resp = requests.post(
            ISSUE_URL,
            headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
            data=json.dumps({
                "room": room,
                "system_unit": system_unit,
                "description": description,
                "reported_by": DISPLAY_NAME,
                "timestamp": time.time(),
            }),
            timeout=5,
        )
        return resp.status_code == 200
    except requests.RequestException:
        return False


# ----------------- CONSOLE HIDING -----------------
def hide_console():
    """Hide the console window so this behaves like a tray-only app, while
    keeping the console technically attached (needed for shutdown detection)."""
    hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE


# ----------------- CLEAN SHUTDOWN DETECTION -----------------
def console_ctrl_handler(ctrl_type):
    if ctrl_type in (
        win32con.CTRL_C_EVENT, win32con.CTRL_BREAK_EVENT, win32con.CTRL_CLOSE_EVENT,
        win32con.CTRL_LOGOFF_EVENT, win32con.CTRL_SHUTDOWN_EVENT,
    ):
        send_shutdown_signal()
        if tray_icon:
            tray_icon.stop()
        return False
    return False


# ----------------- REPORT-A-PROBLEM FORM -----------------
def open_report_form(icon=None, item=None):
    """Opens a small form so anyone can flag an issue with ANY PC in the room,
    not just the one running this agent."""
    def submit():
        room = room_entry.get().strip()
        unit = unit_entry.get().strip()
        desc = desc_text.get("1.0", tk.END).strip()

        if not room or not unit or not desc:
            messagebox.showwarning("Missing info", "Please fill in Room, System Unit, and a description.")
            return

        ok = send_issue_report(room, unit, desc)
        if ok:
            messagebox.showinfo("Reported", "Problem reported. A technician will see it on the dashboard.")
            form.destroy()
        else:
            messagebox.showerror("Failed", "Could not reach the server. Check your network and try again.")

    form = tk.Tk()
    form.title("Report a Problem")
    form.geometry("360x320")
    form.resizable(False, False)
    form.attributes("-topmost", True)

    tk.Label(form, text="Room", anchor="w").pack(fill="x", padx=12, pady=(12, 0))
    room_entry = tk.Entry(form)
    room_entry.insert(0, ROOM)
    room_entry.pack(fill="x", padx=12)

    tk.Label(form, text="System Unit (any PC in the room)", anchor="w").pack(fill="x", padx=12, pady=(10, 0))
    unit_entry = tk.Entry(form)
    unit_entry.insert(0, SYSTEM_UNIT)
    unit_entry.pack(fill="x", padx=12)

    tk.Label(form, text="What's wrong?", anchor="w").pack(fill="x", padx=12, pady=(10, 0))
    desc_text = tk.Text(form, height=6)
    desc_text.pack(fill="x", padx=12)

    tk.Button(form, text="Submit Report", command=submit, bg="#4f8cff", fg="white").pack(pady=14)

    form.mainloop()


def open_dashboard(icon=None, item=None):
    webbrowser.open(SERVER_BASE)


def quit_app(icon, item):
    send_shutdown_signal()
    icon.stop()


# ----------------- TRAY ICON -----------------
def make_tray_image():
    """Simple generated icon — a blue circle — so no external image file is needed."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((4, 4, 60, 60), fill=(79, 140, 255, 255))
    return img


def reporting_loop():
    """Single unified loop — this replaces what used to be two independent
    threads (a full-report loop + a separate fast peripheral watcher), which
    could race each other and send conflicting snapshots out of order,
    causing exactly the kind of inconsistent state (e.g. "Keyboard missing"
    and the Keyboard chip both showing at once) that a race produces. Now
    there's only ever one report in flight at a time: this loop wakes up
    every DEVICE_WATCH_INTERVAL seconds, checks just the peripheral list
    (cheap), and sends a full report immediately if anything changed OR if
    REPORT_INTERVAL has elapsed since the last report — whichever comes first.

    A single WMI connection is created once and reused for the life of this
    thread, rather than reconnecting on every call — reconnecting every
    couple of seconds indefinitely can exhaust COM resources over a long
    uptime and trigger transient errors like WBEM_E_CALL_CANCELLED. If the
    connection itself ever goes bad, it's recreated automatically. Any other
    transient error is logged and the loop keeps going rather than dying
    silently and leaving the agent looking alive but reporting nothing."""
    # WMI uses COM, and COM must be initialized separately on every thread
    # that touches it — this thread doesn't get it for free like the main
    # thread does, so without this, every WMI call here raises
    # x_wmi_uninitialised_thread.
    pythoncom.CoInitialize()
    last_report_time = 0
    last_devices_seen = None
    wmi_conn = wmi.WMI()
    try:
        while True:
            time.sleep(DEVICE_WATCH_INTERVAL)
            try:
                current_devices = get_connected_devices(wmi_conn)
                current_keys = {(d["type"], d["name"]) for d in current_devices}
                devices_changed = last_devices_seen is not None and current_keys != last_devices_seen
                last_devices_seen = current_keys

                due_for_heartbeat = (time.time() - last_report_time) >= REPORT_INTERVAL

                if devices_changed or due_for_heartbeat:
                    send_report(build_payload(wmi_conn))
                    last_report_time = time.time()
            except Exception as e:
                print(f"[ERROR] Reporting cycle failed, will retry next tick: {e}")
                # The WMI connection itself may have gone bad (e.g. after a
                # COM error) — recreate it so the next tick starts fresh
                # instead of repeating the same failure forever.
                try:
                    wmi_conn = wmi.WMI()
                except Exception as reconnect_error:
                    print(f"[ERROR] Could not reconnect to WMI: {reconnect_error}")
    finally:
        pythoncom.CoUninitialize()


def main():
    global tray_icon
    hide_console()
    win32api.SetConsoleCtrlHandler(console_ctrl_handler, True)

    print(f"Starting agent for device: {DEVICE_ID}")

    # Reporting runs in the background; the tray icon owns the main thread.
    threading.Thread(target=reporting_loop, daemon=True).start()

    menu = pystray.Menu(
        pystray.MenuItem(f"{DISPLAY_NAME} — {ROOM} / {SYSTEM_UNIT}", None, enabled=False),
        pystray.MenuItem("Open Dashboard", open_dashboard),
        pystray.MenuItem("Report a Problem...", open_report_form),
        pystray.MenuItem("Quit", quit_app),
    )
    tray_icon = pystray.Icon("device_tracker", make_tray_image(), "Device Tracker Agent", menu)
    tray_icon.run()


if __name__ == "__main__":
    main()