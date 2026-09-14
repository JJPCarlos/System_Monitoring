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

STALE_THRESHOLD_SECONDS = 15   # ~3x the agent's default 5s report interval
STATUS_CHECK_INTERVAL = 5

app = FastAPI()

# In-memory status tracker: device_id -> {"last_seen": ts, "status": "online"/"offline"/"crashed"}
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
    conn.commit()
    conn.close()

init_db()

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
        d["missing_components"] = compute_missing(d["device_id"], d.get("internal", []))
    return devices

# ----------------- BASELINE (expected internal components) -----------------
def component_key(comp: dict) -> str:
    """Identity key for a component. RAM is keyed by physical slot (so a pulled
    stick is caught even if another stick of the same size is still in a different
    slot); everything else is keyed by type+name."""
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

def compute_missing(device_id: str, current_internal: list) -> list:
    baseline = get_baseline(device_id)
    if not baseline:
        return []
    current_keys = {component_key(c) for c in current_internal}
    return [b for b in baseline if component_key(b) not in current_keys]

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
    while True:
        await asyncio.sleep(STATUS_CHECK_INTERVAL)
        now = time.time()
        for device_id, info in device_status.items():
            if info["status"] == "online" and (now - info["last_seen"]) > STALE_THRESHOLD_SECONDS:
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
    save_report(payload)

    device_id = payload["device_id"]
    device_status[device_id] = {"last_seen": payload["timestamp"], "status": "online"}
    payload["status"] = "online"

    # Auto-capture a baseline the very first time we ever see this device
    if get_baseline(device_id) is None and payload.get("internal"):
        save_baseline(device_id, payload["internal"])

    payload["missing_components"] = compute_missing(device_id, payload.get("internal", []))

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

    save_baseline(device_id, match.get("internal", []))
    return {"status": "ok", "baseline_size": len(match.get("internal", []))}

@app.get("/api/devices")
async def list_devices():
    return get_latest_per_device()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    await websocket.send_json({"type": "snapshot", "data": get_latest_per_device()})
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html_path = Path(__file__).parent / "dashboard.html"
    return FileResponse(html_path)