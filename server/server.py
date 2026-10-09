"""
Central Device Tracking Server
--------------------------------
Receives reports from Windows agents, stores them in SQLite,
tracks online/offline/crashed status, records a baseline of each
PC's internal components, and flags anything missing on later boots.
Pushes live updates to any connected dashboard via WebSocket.

Requirements:
    pip install fastapi uvicorn

Run:
    uvicorn server:app --host 0.0.0.0 --port 8000
"""

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import List

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, FileResponse

API_KEY = "changeme-shared-secret"   # <-- must match agent's API_KEY
DB_PATH = Path(__file__).parent / "reports.db"

STALE_THRESHOLD_SECONDS = 15      # ~3x the agent's default 5s report interval — flips to "unresponsive"
CRASHED_THRESHOLD_SECONDS = 45    # ~9x the report interval — escalates to "crashed"
STATUS_CHECK_INTERVAL = 5

app = FastAPI()

# In-memory status tracker: device_id -> {"last_seen": ts, "status": "online"/"offline"/"unresponsive"/"crashed"}
device_status = {}

# ----------------- DATABASE -----------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            hostname TEXT,
            os TEXT,
            timestamp REAL,
            payload TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS baselines (
            device_id TEXT PRIMARY KEY,
            components TEXT,
            captured_at REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room TEXT,
            system_unit TEXT,
            description TEXT,
            reported_by TEXT,
            timestamp REAL,
            resolved INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS device_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            hostname TEXT,
            event_type TEXT,
            component_type TEXT,
            component_name TEXT,
            timestamp REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS maintenance (
            device_id TEXT PRIMARY KEY,
            under_maintenance INTEGER,
            set_by TEXT,
            timestamp REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS repair_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            hostname TEXT,
            reported_problem TEXT,
            actions_taken TEXT,
            part_type TEXT,
            model_before TEXT,
            model_after TEXT,
            repair_status TEXT,
            technician TEXT,
            timestamp REAL
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ----------------- REPAIR TICKETS (structured maintenance history, separate
# from the lightweight "Log a Repair" note — this is the full ticket system:
# reported problem, actions taken, part replaced with before/after model,
# and a status that can be updated over time) -----------------
REPAIR_STATUSES = ("Pending", "In Progress", "Completed")

def save_repair_ticket(device_id: str, hostname: str, reported_problem: str, actions_taken: str,
                        part_type: str, model_before: str, model_after: str,
                        repair_status: str, technician: str, timestamp: float) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        """INSERT INTO repair_tickets
           (device_id, hostname, reported_problem, actions_taken, part_type, model_before, model_after, repair_status, technician, timestamp)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (device_id, hostname, reported_problem, actions_taken, part_type, model_before, model_after, repair_status, technician, timestamp),
    )
    conn.commit()
    ticket_id = cur.lastrowid
    conn.close()
    return ticket_id

def _ticket_row_to_dict(r) -> dict:
    return {
        "id": r[0], "device_id": r[1], "hostname": r[2], "reported_problem": r[3],
        "actions_taken": r[4], "part_type": r[5], "model_before": r[6], "model_after": r[7],
        "repair_status": r[8], "technician": r[9], "timestamp": r[10],
    }

def get_repair_tickets(device_id: str = None, status: str = None) -> list:
    conn = sqlite3.connect(DB_PATH)
    query = "SELECT id, device_id, hostname, reported_problem, actions_taken, part_type, model_before, model_after, repair_status, technician, timestamp FROM repair_tickets"
    clauses, params = [], []
    if device_id:
        clauses.append("device_id = ?")
        params.append(device_id)
    if status:
        clauses.append("repair_status = ?")
        params.append(status)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY timestamp DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [_ticket_row_to_dict(r) for r in rows]

def get_repair_ticket(ticket_id: int):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id, device_id, hostname, reported_problem, actions_taken, part_type, model_before, model_after, repair_status, technician, timestamp FROM repair_tickets WHERE id = ?",
        (ticket_id,),
    ).fetchone()
    conn.close()
    return _ticket_row_to_dict(row) if row else None

def update_repair_ticket_status(ticket_id: int, status: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE repair_tickets SET repair_status = ? WHERE id = ?", (status, ticket_id))
    conn.commit()
    conn.close()

def complete_open_tickets(device_id: str, note: str) -> list:
    """Mark every Pending / In Progress ticket for a device as Completed.
    If a note is given it is appended to the ticket's actions_taken."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, actions_taken FROM repair_tickets WHERE device_id = ? AND repair_status != 'Completed'",
        (device_id,),
    ).fetchall()
    ids = []
    for tid, actions in rows:
        new_actions = "\n".join(x for x in (actions, note) if x)
        conn.execute("UPDATE repair_tickets SET repair_status = 'Completed', actions_taken = ? WHERE id = ?", (new_actions, tid))
        ids.append(tid)
    conn.commit()
    conn.close()
    return ids

# ----------------- MAINTENANCE FLAG (persisted so every dashboard viewer agrees) -----------------
def get_maintenance(device_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT under_maintenance FROM maintenance WHERE device_id = ?", (device_id,)).fetchone()
    conn.close()
    return bool(row[0]) if row else False

def set_maintenance(device_id: str, flag: bool, set_by: str, timestamp: float):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO maintenance (device_id, under_maintenance, set_by, timestamp) VALUES (?, ?, ?, ?)",
        (device_id, 1 if flag else 0, set_by, timestamp),
    )
    conn.commit()
    conn.close()

def save_report(payload: dict):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO reports (device_id, hostname, os, timestamp, payload) VALUES (?, ?, ?, ?, ?)",
        (payload["device_id"], payload["hostname"], payload["os"], payload["timestamp"], json.dumps(payload)),
    )
    conn.commit()
    conn.close()

def get_latest_per_device():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT payload FROM reports r
        WHERE id = (SELECT MAX(id) FROM reports WHERE device_id = r.device_id)
    """).fetchall()
    conn.close()
    devices = [json.loads(r[0]) for r in rows]
    for d in devices:
        # If we have no in-memory status yet for this device (e.g. right after a
        # server restart, before it has reported back in), assume offline rather
        # than online — we haven't actually heard from it since starting up.
        info = device_status.get(d["device_id"])
        d["status"] = info["status"] if info else "offline"
        d["missing_components"] = compute_missing(d["device_id"], combined_components(d))
        # Prefer the agent's own computed flags (which may be pinned to a
        # specific device's VID/PID) over recomputing generically here —
        # only fall back for older reports that predate that feature.
        d["peripheral_flags"] = d.get("peripheral_flags") or compute_peripheral_flags(d.get("devices", []))
        d["under_maintenance"] = get_maintenance(d["device_id"])
    return devices

# ----------------- BASELINE (expected internal + peripheral components) -----------------
def combined_components(payload: dict) -> list:
    """Internal parts (RAM/CPU/GPU/drives) + peripherals (keyboard/mouse/USB/etc)
    treated as one set for baseline comparison, so a missing keyboard or mouse
    is caught the same way a missing RAM stick is."""
    return (payload.get("internal") or []) + (payload.get("devices") or [])

def component_key(comp: dict) -> str:
    """Identity key for a component. RAM is keyed by physical slot (so a pulled
    stick is caught even if another stick of the same size is still in a different
    slot); everything else (including peripherals like Keyboard/Mouse) is keyed
    by type+name."""
    if comp.get("type") == "RAM Stick":
        return f"RAM:{comp.get('slot', '?')}"
    return f"{comp.get('type')}:{comp.get('name')}"

def get_baseline(device_id: str):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT components FROM baselines WHERE device_id = ?", (device_id,)).fetchone()
    conn.close()
    return json.loads(row[0]) if row else None

def save_baseline(device_id: str, components: list):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO baselines (device_id, components, captured_at) VALUES (?, ?, ?)",
        (device_id, json.dumps(components), time.time()),
    )
    conn.commit()
    conn.close()

def compute_missing(device_id: str, current_components: list) -> list:
    baseline = get_baseline(device_id)
    if not baseline:
        return []
    current_keys = {component_key(c) for c in current_components}
    return [b for b in baseline if component_key(b) not in current_keys]

def compute_peripheral_flags(current_devices: list) -> dict:
    """Quick live presence check for the peripherals people care about most —
    independent of baseline history, so it works even on a device's very first report."""
    types_present = {d.get("type") for d in current_devices}
    return {
        "has_keyboard": "Keyboard" in types_present,
        "has_mouse": "Mouse" in types_present,
    }

# ----------------- PROBLEM REPORTS (any PC in a room, not just self) -----------------
def save_issue(room: str, system_unit: str, description: str, reported_by: str, timestamp: float) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO issues (room, system_unit, description, reported_by, timestamp, resolved) VALUES (?, ?, ?, ?, ?, 0)",
        (room, system_unit, description, reported_by, timestamp),
    )
    conn.commit()
    issue_id = cur.lastrowid
    conn.close()
    return issue_id

def get_issues(include_resolved: bool = False):
    conn = sqlite3.connect(DB_PATH)
    query = "SELECT id, room, system_unit, description, reported_by, timestamp, resolved FROM issues"
    if not include_resolved:
        query += " WHERE resolved = 0"
    query += " ORDER BY timestamp DESC"
    rows = conn.execute(query).fetchall()
    conn.close()
    return [
        {
            "id": r[0], "room": r[1], "system_unit": r[2], "description": r[3],
            "reported_by": r[4], "timestamp": r[5], "resolved": bool(r[6]),
        }
        for r in rows
    ]

def resolve_issue(issue_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE issues SET resolved = 1 WHERE id = ?", (issue_id,))
    conn.commit()
    conn.close()

# ----------------- DEVICE CONNECT/DISCONNECT HISTORY -----------------
def get_previous_devices(device_id: str):
    """The peripherals list from this device's last report, BEFORE the current
    one is saved — used to detect what changed. Returns None if this is the
    device's very first report (nothing to diff against yet)."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT payload FROM reports WHERE device_id = ? ORDER BY id DESC LIMIT 1",
        (device_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return json.loads(row[0]).get("devices", [])

def log_device_event(device_id: str, hostname: str, event_type: str, component: dict, timestamp: float):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO device_events (device_id, hostname, event_type, component_type, component_name, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
        (device_id, hostname, event_type, component.get("type"), component.get("name"), timestamp),
    )
    conn.commit()
    conn.close()

def diff_and_log_devices(device_id: str, hostname: str, previous: list, current: list, timestamp: float) -> list:
    """Compares the peripherals list between two reports and logs a
    connected/disconnected event for anything that changed. Returns the list
    of events so the caller can broadcast them live."""
    prev_keys = {component_key(c): c for c in previous}
    curr_keys = {component_key(c): c for c in current}

    events = []
    for key, comp in curr_keys.items():
        if key not in prev_keys:
            log_device_event(device_id, hostname, "connected", comp, timestamp)
            events.append({"device_id": device_id, "hostname": hostname, "event_type": "connected",
                            "component_type": comp.get("type"), "component_name": comp.get("name"), "timestamp": timestamp})
    for key, comp in prev_keys.items():
        if key not in curr_keys:
            log_device_event(device_id, hostname, "disconnected", comp, timestamp)
            events.append({"device_id": device_id, "hostname": hostname, "event_type": "disconnected",
                            "component_type": comp.get("type"), "component_name": comp.get("name"), "timestamp": timestamp})
    return events

def get_device_history(device_id: str, limit: int = 100):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT event_type, component_type, component_name, timestamp FROM device_events "
        "WHERE device_id = ? ORDER BY timestamp DESC LIMIT ?",
        (device_id, limit),
    ).fetchall()
    conn.close()
    return [{"event_type": r[0], "component_type": r[1], "component_name": r[2], "timestamp": r[3]} for r in rows]

# ----------------- WEBSOCKET CONNECTIONS -----------------
class ConnectionManager:
    def __init__(self):
        self.active: List[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, message: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

manager = ConnectionManager()

# ----------------- BACKGROUND: detect crashed devices -----------------
async def watch_for_crashes():
    """Escalates a silent device: online -> unresponsive (short silence, could be
    a network blip or a busy machine) -> crashed (prolonged silence, very likely dead)."""
    while True:
        await asyncio.sleep(STATUS_CHECK_INTERVAL)
        now = time.time()
        for device_id, info in device_status.items():
            elapsed = now - info["last_seen"]

            if info["status"] == "online" and elapsed > STALE_THRESHOLD_SECONDS:
                info["status"] = "unresponsive"
                await manager.broadcast({
                    "type": "status",
                    "data": {"device_id": device_id, "status": "unresponsive"}
                })

            elif info["status"] == "unresponsive" and elapsed > CRASHED_THRESHOLD_SECONDS:
                info["status"] = "crashed"
                await manager.broadcast({
                    "type": "status",
                    "data": {"device_id": device_id, "status": "crashed"}
                })

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(watch_for_crashes())

# ----------------- ROUTES -----------------
@app.post("/api/report")
async def receive_report(request: Request):
    if request.headers.get("X-API-Key") != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

    payload = await request.json()
    payload.setdefault("timestamp", time.time())

    device_id = payload["device_id"]
    hostname = payload.get("hostname", device_id)

    # Grab the previous report's peripherals BEFORE saving this new one, so we
    # can diff what changed (connected/disconnected) and log it to history.
    previous_devices = get_previous_devices(device_id)

    save_report(payload)

    device_status[device_id] = {"last_seen": payload["timestamp"], "status": "online"}
    payload["status"] = "online"

    # Auto-capture a baseline the very first time we ever see this device
    # (covers internal parts AND peripherals like keyboard/mouse)
    combined = combined_components(payload)
    if get_baseline(device_id) is None and combined:
        save_baseline(device_id, combined)

    payload["missing_components"] = compute_missing(device_id, combined)
    # Prefer the agent's own computed flags (which may be pinned to a
    # specific device's VID/PID) over recomputing generically here — only
    # fall back for older agents that predate that feature.
    payload["peripheral_flags"] = payload.get("peripheral_flags") or compute_peripheral_flags(payload.get("devices", []))
    payload["under_maintenance"] = get_maintenance(device_id)

    # Log + broadcast any peripheral connect/disconnect since the last report
    if previous_devices is not None:
        events = diff_and_log_devices(
            device_id, hostname, previous_devices, payload.get("devices", []), payload["timestamp"]
        )
        for event in events:
            await manager.broadcast({"type": "device_event", "data": event})

    await manager.broadcast({"type": "update", "data": payload})
    return {"status": "ok"}

@app.post("/api/shutdown")
async def receive_shutdown(request: Request):
    if request.headers.get("X-API-Key") != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

    body = await request.json()
    device_id = body.get("device_id")
    if device_id:
        device_status[device_id] = {"last_seen": time.time(), "status": "offline"}
        await manager.broadcast({
            "type": "status",
            "data": {"device_id": device_id, "status": "offline"}
        })
    return {"status": "ok"}

@app.post("/api/baseline/{device_id}/reset")
async def reset_baseline(device_id: str, request: Request):
    """Call this after an intentional hardware change, so the new configuration
    becomes the new 'expected' baseline instead of showing as missing forever."""
    if request.headers.get("X-API-Key") != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

    devices = get_latest_per_device()
    match = next((d for d in devices if d["device_id"] == device_id), None)
    if not match:
        raise HTTPException(status_code=404, detail="No recent report for this device")

    save_baseline(device_id, combined_components(match))
    return {"status": "ok", "baseline_size": len(combined_components(match))}

@app.post("/api/repair-log/{device_id}")
async def log_repair(device_id: str, request: Request):
    """Technician-only: log that something was fixed/replaced on this device.
    Protected the same way as the other admin actions (API key) — only
    someone with the shared secret (i.e. a technician, not a random dashboard
    viewer) can add an entry. Reuses the same device_events table/history as
    connect/disconnect events, tagged with its own event_type so the
    dashboard can style it distinctly."""
    if request.headers.get("X-API-Key") != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

    body = await request.json()
    note = body.get("note", "").strip()
    name = body.get("name", "").strip()
    if not note:
        raise HTTPException(status_code=400, detail="note is required")
    if not name:
        raise HTTPException(status_code=400, detail="name is required")

    devices = get_latest_per_device()
    match = next((d for d in devices if d["device_id"] == device_id), None)
    hostname = match["hostname"] if match else device_id
    timestamp = time.time()

    log_device_event(device_id, hostname, "repaired", {"type": name, "name": note}, timestamp)
    event = {
        "device_id": device_id, "hostname": hostname, "event_type": "repaired",
        "component_type": name, "component_name": note, "timestamp": timestamp,
    }
    await manager.broadcast({"type": "device_event", "data": event})

    # Optional: caller can ask to also save the CURRENT internal components as
    # the new expected baseline in the same request — e.g. after installing a
    # new RAM stick, this stops it from showing as "missing" forever.
    if body.get("update_baseline") and match:
        save_baseline(device_id, combined_components(match))

    return {"status": "ok"}

@app.post("/api/maintenance/{device_id}")
async def set_maintenance_route(device_id: str, request: Request):
    """Technician-only: mark a PC as under maintenance (or clear that flag).
    Persisted server-side so every dashboard viewer sees the same state,
    rather than each browser tracking it independently."""
    if request.headers.get("X-API-Key") != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

    body = await request.json()
    flag = bool(body.get("under_maintenance"))
    name = body.get("name", "").strip()
    reason = body.get("reason", "").strip()
    note = body.get("note", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    # Turning maintenance ON must say why — that reason becomes a ticket.
    if flag and not reason:
        raise HTTPException(status_code=400, detail="reason is required when marking a PC under maintenance")

    devices = get_latest_per_device()
    match = next((d for d in devices if d["device_id"] == device_id), None)
    hostname = match["hostname"] if match else device_id
    timestamp = time.time()

    set_maintenance(device_id, flag, name, timestamp)

    ticket = None
    completed_ids = []
    if flag:
        ticket_id = save_repair_ticket(device_id, hostname, reason, "", "", "", "", "Pending", name, timestamp)
        ticket = get_repair_ticket(ticket_id)
        await manager.broadcast({"type": "repair_ticket", "data": ticket})
    else:
        # Back in service: close this PC's open tickets (optional note appended).
        if body.get("complete_tickets", True):
            completed_ids = complete_open_tickets(device_id, note)
            for tid in completed_ids:
                await manager.broadcast({"type": "repair_ticket", "data": get_repair_ticket(tid)})

    await manager.broadcast({
        "type": "maintenance",
        "data": {"device_id": device_id, "hostname": hostname, "under_maintenance": flag, "set_by": name, "timestamp": timestamp},
    })
    return {"status": "ok", "ticket_id": ticket["id"] if ticket else None, "completed_ticket_ids": completed_ids}

@app.post("/api/repair-tickets/{device_id}")
async def create_repair_ticket(device_id: str, request: Request):
    """Technician-only: open a structured repair ticket for a device —
    reported problem, actions taken, part replaced (with before/after model),
    and a status. Separate from the lightweight /api/repair-log note."""
    if request.headers.get("X-API-Key") != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

    body = await request.json()
    reported_problem = body.get("reported_problem", "").strip()
    actions_taken = body.get("actions_taken", "").strip()
    part_type = body.get("part_type", "").strip()
    model_before = body.get("model_before", "").strip()
    model_after = body.get("model_after", "").strip()
    repair_status = body.get("repair_status", "Pending").strip()
    technician = body.get("technician", "").strip()

    if not reported_problem:
        raise HTTPException(status_code=400, detail="reported_problem is required")
    if not technician:
        raise HTTPException(status_code=400, detail="technician is required")
    if repair_status not in REPAIR_STATUSES:
        raise HTTPException(status_code=400, detail=f"repair_status must be one of {REPAIR_STATUSES}")

    devices = get_latest_per_device()
    match = next((d for d in devices if d["device_id"] == device_id), None)
    hostname = match["hostname"] if match else device_id
    timestamp = time.time()

    ticket_id = save_repair_ticket(
        device_id, hostname, reported_problem, actions_taken,
        part_type, model_before, model_after, repair_status, technician, timestamp,
    )
    ticket = get_repair_ticket(ticket_id)
    await manager.broadcast({"type": "repair_ticket", "data": ticket})
    return {"status": "ok", "id": ticket_id}

@app.get("/api/repair-tickets")
async def list_repair_tickets(status: str = None):
    """Fleet-wide ticket list, optionally filtered by status — used by the
    Repair Tracking page's "All PCs" view and report generation."""
    return get_repair_tickets(status=status)

@app.get("/api/repair-tickets/{device_id}")
async def list_repair_tickets_for_device(device_id: str, status: str = None):
    """One device's maintenance history."""
    return get_repair_tickets(device_id=device_id, status=status)

@app.post("/api/repair-tickets/{ticket_id}/status")
async def update_repair_ticket_status_route(ticket_id: int, request: Request):
    if request.headers.get("X-API-Key") != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

    body = await request.json()
    status = body.get("repair_status", "").strip()
    if status not in REPAIR_STATUSES:
        raise HTTPException(status_code=400, detail=f"repair_status must be one of {REPAIR_STATUSES}")

    ticket = get_repair_ticket(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    update_repair_ticket_status(ticket_id, status)
    ticket["repair_status"] = status
    await manager.broadcast({"type": "repair_ticket", "data": ticket})
    return {"status": "ok"}

@app.get("/api/devices")
async def list_devices():
    return get_latest_per_device()

@app.get("/api/history/{device_id}")
async def device_history(device_id: str, limit: int = 100):
    return get_device_history(device_id, limit)

@app.post("/api/report-issue")
async def report_issue(request: Request):
    """Anyone (any agent's tray menu) can report a problem with ANY PC in a
    room, not just the one they're sitting at."""
    if request.headers.get("X-API-Key") != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

    body = await request.json()
    room = body.get("room", "").strip()
    system_unit = body.get("system_unit", "").strip()
    description = body.get("description", "").strip()
    reported_by = body.get("reported_by", "Unknown")
    timestamp = body.get("timestamp", time.time())

    if not room or not system_unit or not description:
        raise HTTPException(status_code=400, detail="room, system_unit, and description are required")

    issue_id = save_issue(room, system_unit, description, reported_by, timestamp)
    issue = {
        "id": issue_id, "room": room, "system_unit": system_unit, "description": description,
        "reported_by": reported_by, "timestamp": timestamp, "resolved": False,
    }
    await manager.broadcast({"type": "issue", "data": issue})
    return {"status": "ok", "id": issue_id}

@app.get("/api/issues")
async def list_issues(include_resolved: bool = False):
    return get_issues(include_resolved)

@app.post("/api/issues/{issue_id}/resolve")
async def resolve_issue_route(issue_id: int, request: Request):
    if request.headers.get("X-API-Key") != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    resolve_issue(issue_id)
    await manager.broadcast({"type": "issue_resolved", "data": {"id": issue_id}})
    return {"status": "ok"}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    await websocket.send_json({"type": "snapshot", "data": get_latest_per_device()})
    await websocket.send_json({"type": "issues_snapshot", "data": get_issues()})
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html_path = Path(__file__).parent / "dashboard.html"
    return FileResponse(html_path)