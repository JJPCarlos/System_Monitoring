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
import base64
import csv
import hashlib
import hmac
import io
import json
import secrets
import sqlite3
import time
from pathlib import Path
from typing import List

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException, Depends
from fastapi.responses import HTMLResponse, FileResponse, Response

API_KEY = "changeme-shared-secret"   # <-- must match agent's API_KEY (agents only; the dashboard uses login tokens)
TOKEN_TTL_HOURS = 12                 # how long a dashboard login stays valid
MIN_PASSWORD_LENGTH = 6
DEFAULT_ADMIN = ("admin", "admin123")   # created on first run if there are no accounts — change it after logging in
SECRET_PATH = Path(__file__).parent / "secret.key"   # signs login tokens; created on first run
DB_PATH = Path(__file__).parent / "reports.db"
REPORT_RETENTION_DAYS = 7                      # reports older than this are archived to CSV, then deleted
ARCHIVE_DIR = Path(__file__).parent / "archive"  # one CSV per day: reports-YYYY-MM-DD.csv
PRUNE_EVERY_SECONDS = 3600
PART_LOG_PATH = ARCHIVE_DIR / "part-replacements.csv"   # permanent record of replaced parts (never pruned)

DISK_WARN_PERCENT = 75    # C: drive this full (or more) = "disk almost full" alert (75% used = 25% or less free)
DISK_CLEAR_PERCENT = 73   # must drop below this before it can alert again (stops on/off flapping around 75%)

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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS inventory_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            category TEXT,
            quantity INTEGER DEFAULT 0,
            unit TEXT,
            notes TEXT,
            created_by TEXT,
            updated_by TEXT,
            updated_at REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS inventory_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER,
            item_name TEXT,
            change INTEGER,
            qty_after INTEGER,
            reason TEXT,
            by_user TEXT,
            timestamp REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS inventory_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER,
            device_id TEXT,
            hostname TEXT,
            task_problem TEXT,
            note TEXT,
            status TEXT,
            proposed_by TEXT,
            created_at REAL,
            decided_by TEXT,
            decided_at REAL,
            decision_note TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS inventory_proposal_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_id INTEGER,
            item_id INTEGER,
            item_name TEXT,
            quantity INTEGER
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_prop_items ON inventory_proposal_items(proposal_id)")
    # Speeds up "latest report per PC" and per-PC history as the table grows.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reports_device ON reports(device_id, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reports_time ON reports(timestamp)")
    # Assigned-task columns (added later, so migrate older databases in place).
    existing = {r[1] for r in conn.execute("PRAGMA table_info(repair_tickets)").fetchall()}
    for col, typ in (("assigned_to", "TEXT"), ("assigned_by", "TEXT"), ("completed_at", "REAL"), ("completed_by", "TEXT"), ("reassigned_from", "TEXT")):
        if col not in existing:
            conn.execute(f"ALTER TABLE repair_tickets ADD COLUMN {col} {typ}")
    # Inventory: price per unit (pesos) and a low-stock level per item; the stock log also
    # records the price at the time, the cost, and which PC / task the stock went to.
    for table, cols in (("inventory_items", (("unit_price", "REAL DEFAULT 0"), ("min_qty", "INTEGER DEFAULT 0"),
                                             ("board_platform", "TEXT"), ("ram_type", "TEXT"))),
                        ("inventory_log", (("unit_price", "REAL"), ("cost", "REAL"), ("device_id", "TEXT"),
                                           ("hostname", "TEXT"), ("ticket_id", "INTEGER"), ("task", "TEXT")))):
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col, typ in cols:
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_inv_log_time ON inventory_log(timestamp)")
    # PC incidents for the dashboard graph: crashes, keyboard/mouse lost, internal part missing.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            hostname TEXT,
            kind TEXT,
            detail TEXT,
            timestamp REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_incidents_time ON incidents(timestamp)")
    # Room filter buttons on Monitoring System: a label + the ROOM value (from each PC's config.toml)
    # it looks for. Seeded once with the original two rooms.
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='room_groups'").fetchone():
        conn.execute("CREATE TABLE room_groups (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT, room TEXT, created_by TEXT, created_at REAL)")
        conn.executemany("INSERT INTO room_groups (label, room, created_by, created_at) VALUES (?, ?, 'system', ?)",
                         [("Computer Center 1", "201", time.time()), ("Computer Center 2", "202", time.time())])
    # Keyboard/Mouse used to be saved in baselines; they now have their own live check.
    for did, comps in conn.execute("SELECT device_id, components FROM baselines").fetchall():
        try:
            data = json.loads(comps)
        except Exception:
            continue
        kept = [c for c in data if c.get("type") not in ("Keyboard", "Mouse")]
        if len(kept) != len(data):
            conn.execute("UPDATE baselines SET components = ? WHERE device_id = ?", (json.dumps(kept), did))
    conn.commit()
    conn.close()

init_db()

# ----------------- REPAIR TICKETS (structured maintenance history, separate
# from the lightweight "Log a Repair" note — this is the full ticket system:
# reported problem, actions taken, part replaced with before/after model,
# and a status that can be updated over time) -----------------
REPAIR_STATUSES = ("Pending", "In Progress", "Completed")

TICKET_COLS = ("id, device_id, hostname, reported_problem, actions_taken, part_type, model_before, model_after, "
               "repair_status, technician, timestamp, assigned_to, assigned_by, completed_at, completed_by, reassigned_from")

def save_repair_ticket(device_id: str, hostname: str, reported_problem: str, actions_taken: str,
                        part_type: str, model_before: str, model_after: str,
                        repair_status: str, technician: str, timestamp: float,
                        assigned_to: str = None, assigned_by: str = None) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        """INSERT INTO repair_tickets
           (device_id, hostname, reported_problem, actions_taken, part_type, model_before, model_after, repair_status, technician, timestamp, assigned_to, assigned_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (device_id, hostname, reported_problem, actions_taken, part_type, model_before, model_after, repair_status, technician, timestamp, assigned_to, assigned_by),
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
        "assigned_to": r[11], "assigned_by": r[12], "completed_at": r[13], "completed_by": r[14],
        "reassigned_from": r[15],
    }

def get_repair_tickets(device_id: str = None, status: str = None) -> list:
    conn = sqlite3.connect(DB_PATH)
    query = f"SELECT {TICKET_COLS} FROM repair_tickets"
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
    row = conn.execute(f"SELECT {TICKET_COLS} FROM repair_tickets WHERE id = ?", (ticket_id,)).fetchone()
    conn.close()
    return _ticket_row_to_dict(row) if row else None

def finish_repair_ticket(ticket_id: int, status: str, by: str, actions_taken=None, part_type=None,
                         model_before=None, model_after=None):
    """Apply a technician's update. Completing stamps completed_at / completed_by;
    moving back out of Completed clears them."""
    conn = sqlite3.connect(DB_PATH)
    sets, params = ["repair_status = ?"], [status]
    for col, val in (("actions_taken", actions_taken), ("part_type", part_type),
                     ("model_before", model_before), ("model_after", model_after)):
        if val is not None:
            sets.append(f"{col} = ?"); params.append(val)
    if status == "Completed":
        sets += ["completed_at = ?", "completed_by = ?"]; params += [time.time(), by]
    else:
        sets += ["completed_at = NULL", "completed_by = NULL"]
    params.append(ticket_id)
    conn.execute(f"UPDATE repair_tickets SET {', '.join(sets)} WHERE id = ?", params)
    conn.commit()
    conn.close()

PART_LOG_HEADER = ["date", "time", "pc", "device_id", "part_type", "model_before", "model_after",
                   "technician", "task", "actions_taken", "ticket_id"]

def log_part_replacement(ticket: dict):
    """Append one row to the permanent part-replacement CSV when a task is completed with a
    part filled in. Append-only and never pruned, so it stays a record even if the task is
    deleted later. If the file can't be written (e.g. it is open in Excel) the row goes to a
    side file instead, so the record is never lost and never blocks completing the task."""
    if not (ticket.get("part_type") or "").strip():
        return
    when = time.localtime(ticket.get("completed_at") or time.time())
    row = [time.strftime("%Y-%m-%d", when), time.strftime("%H:%M:%S", when),
           ticket.get("hostname") or "", ticket.get("device_id") or "",
           ticket.get("part_type") or "", ticket.get("model_before") or "", ticket.get("model_after") or "",
           ticket.get("completed_by") or "", ticket.get("reported_problem") or "",
           ticket.get("actions_taken") or "", ticket.get("id")]
    ARCHIVE_DIR.mkdir(exist_ok=True)
    for path in (PART_LOG_PATH, PART_LOG_PATH.with_name("part-replacements-unsaved.csv")):
        try:
            new_file = not path.exists() or path.stat().st_size == 0
            with open(path, "a", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                if new_file:
                    w.writerow(PART_LOG_HEADER)
                w.writerow(row)
            if path is not PART_LOG_PATH:
                print(f"[WARN] {PART_LOG_PATH.name} could not be written (open in another program?) — saved to {path.name}")
            return
        except OSError as e:
            print(f"[WARN] Could not write {path.name}: {e}")

def update_repair_ticket_status(ticket_id: int, status: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE repair_tickets SET repair_status = ? WHERE id = ?", (status, ticket_id))
    conn.commit()
    conn.close()

def complete_open_tickets(device_id: str, note: str, by: str = None) -> list:
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
        conn.execute("UPDATE repair_tickets SET repair_status = 'Completed', actions_taken = ?, completed_at = ?, completed_by = ? WHERE id = ?",
                     (new_actions, time.time(), by, tid))
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

ARCHIVE_COLUMNS = ["time", "device_id", "hostname", "room", "system_unit", "os", "cpu_percent", "ram_percent", "disk_percent"]

def prune_old_reports(retention_days: int = REPORT_RETENTION_DAYS) -> int:
    """Write reports older than retention_days to daily CSV files, then delete them.
    Each PC's newest report is always kept (the dashboard needs it). Runs in a
    worker thread so it never blocks incoming reports. Returns rows archived."""
    import csv
    cutoff = time.time() - retention_days * 86400
    ARCHIVE_DIR.mkdir(exist_ok=True)
    total = 0
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        while True:
            rows = conn.execute(
                """SELECT id, device_id, hostname, os, timestamp, payload FROM reports
                   WHERE timestamp < ? AND id NOT IN (SELECT MAX(id) FROM reports GROUP BY device_id)
                   ORDER BY id LIMIT 5000""", (cutoff,)).fetchall()
            if not rows:
                break
            by_day = {}
            for rid, dev, host, os_, ts, payload in rows:
                try:
                    p = json.loads(payload)
                except Exception:
                    p = {}
                hw = p.get("hardware") or {}
                day = time.strftime("%Y-%m-%d", time.localtime(ts or 0))
                by_day.setdefault(day, []).append([
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts or 0)), dev, host,
                    p.get("room", ""), p.get("system_unit", ""), os_,
                    hw.get("cpu_percent", ""), hw.get("ram_percent", ""), hw.get("disk_percent", ""),
                ])
            for day, out in by_day.items():
                path = ARCHIVE_DIR / f"reports-{day}.csv"
                new_file = not path.exists()
                with open(path, "a", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    if new_file:
                        w.writerow(ARCHIVE_COLUMNS)
                    w.writerows(out)
            # Only delete once the CSV rows are safely written.
            conn.executemany("DELETE FROM reports WHERE id = ?", [(r[0],) for r in rows])
            conn.commit()
            total += len(rows)
    finally:
        conn.close()
    return total

async def prune_loop():
    while True:
        try:
            n = await asyncio.to_thread(prune_old_reports)
            if n:
                print(f"[prune] archived and removed {n} reports older than {REPORT_RETENTION_DAYS} days -> {ARCHIVE_DIR}")
        except Exception as e:
            print(f"[prune] failed: {e}")
        await asyncio.sleep(PRUNE_EVERY_SECONDS)

def get_latest_per_device():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT payload FROM reports
        WHERE id IN (SELECT MAX(id) FROM reports GROUP BY device_id)
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
        d["disk_low"] = disk_is_low(d)
        # Prefer the agent's own computed flags (which may be pinned to a
        # specific device's VID/PID) over recomputing generically here —
        # only fall back for older reports that predate that feature.
        d["peripheral_flags"] = d.get("peripheral_flags") or compute_peripheral_flags(d.get("devices", []))
        d["under_maintenance"] = get_maintenance(d["device_id"])
    return devices

# ----------------- BASELINE (expected internal + peripheral components) -----------------
# Keyboard and Mouse are watched by the dedicated live check (peripheral_flags /
# the agent's KEYBOARD_ID and MOUSE_ID), so they are NOT part of the saved baseline.
LIVE_CHECKED_TYPES = {"Keyboard", "Mouse"}

def combined_components(payload: dict) -> list:
    """Internal parts (RAM/CPU/GPU/drives) + other peripherals (USB/etc) treated as
    one set for baseline comparison. Keyboard and Mouse are left out — they have
    their own live check."""
    parts = (payload.get("internal") or []) + (payload.get("devices") or [])
    return [c for c in parts if c.get("type") not in LIVE_CHECKED_TYPES]

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
    # Older saved baselines may still contain keyboard/mouse entries — ignore them.
    return [b for b in baseline if b.get("type") not in LIVE_CHECKED_TYPES and component_key(b) not in current_keys]

def compute_peripheral_flags(current_devices: list) -> dict:
    """Quick live presence check for the peripherals people care about most —
    independent of baseline history, so it works even on a device's very first report."""
    types_present = {d.get("type") for d in current_devices}
    return {
        "has_keyboard": "Keyboard" in types_present,
        "has_mouse": "Mouse" in types_present,
    }

# ----------------- INCIDENTS (history for the dashboard graph) -----------------
# The live status/alerts only exist in memory, so these are saved as they happen:
#   crashed      - went silent without a clean shutdown (logged when it flips to "crashed")
#   keyboard_lost / mouse_lost - was present in the previous report, gone now
#   part_missing - an internal part from the baseline newly missing
#   disk_low     - C: drive reached DISK_WARN_PERCENT used (logged once until it drops below DISK_CLEAR_PERCENT)
# PCs marked Under Maintenance are skipped (technicians pull parts on purpose).
INCIDENT_KINDS = ("crashed", "keyboard_lost", "mouse_lost", "part_missing", "disk_low")

def disk_percent(payload: dict):
    v = (payload.get("hardware") or {}).get("disk_percent")
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None

def disk_is_low(payload: dict) -> bool:
    v = disk_percent(payload)
    return v is not None and v >= DISK_WARN_PERCENT
incident_state = {}   # device_id -> {"missing": set(component keys), "flags": {...}} from the previous report

def log_incident(device_id: str, hostname: str, kind: str, detail: str, timestamp: float = None):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO incidents (device_id, hostname, kind, detail, timestamp) VALUES (?, ?, ?, ?, ?)",
                 (device_id, hostname, kind, detail, timestamp or time.time()))
    conn.commit()
    conn.close()

def _latest_payload(device_id: str):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT payload FROM reports WHERE device_id = ? ORDER BY id DESC LIMIT 1", (device_id,)).fetchone()
    conn.close()
    try:
        return json.loads(row[0]) if row else None
    except Exception:
        return None

def _state_from_payload(device_id: str, payload: dict) -> dict:
    missing = compute_missing(device_id, combined_components(payload))
    flags = payload.get("peripheral_flags") or compute_peripheral_flags(payload.get("devices", []))
    return {"missing": {component_key(m) for m in missing}, "flags": dict(flags), "disk_alerted": disk_is_low(payload)}

def check_incidents(device_id: str, hostname: str, previous_payload, missing: list, flags: dict, timestamp: float,
                    disk: float = None):
    """Compare this report with the previous one and log anything newly wrong.
    Call BEFORE the new report is saved (previous_payload = the last stored report).
    Returns a dict for a live "disk almost full" pop-up when the disk has just crossed the line, else None."""
    prev = incident_state.get(device_id)
    if prev is None and previous_payload:
        prev = _state_from_payload(device_id, previous_payload)   # first report since the server started
    # Disk: alert once on crossing DISK_WARN_PERCENT; re-arm only after it drops below DISK_CLEAR_PERCENT.
    was = bool(prev and prev.get("disk_alerted"))
    if disk is None:
        alerted, crossed = was, False
    elif was:
        alerted, crossed = disk >= DISK_CLEAR_PERCENT, False
    else:
        alerted = crossed = disk >= DISK_WARN_PERCENT
    incident_state[device_id] = {"missing": {component_key(m) for m in missing}, "flags": dict(flags), "disk_alerted": alerted}
    if get_maintenance(device_id):
        return None
    disk_alert = None
    if crossed:
        disk_alert = {"device_id": device_id, "hostname": hostname, "disk_percent": disk, "timestamp": timestamp}
        log_incident(device_id, hostname, "disk_low", f"C: drive {disk:g}% used", timestamp)
    if prev is None:
        return disk_alert
    for m in missing:
        if component_key(m) not in prev["missing"]:
            log_incident(device_id, hostname, "part_missing",
                         f"{m.get('type', '')} — {m.get('name', '')}" + (f" (slot {m['slot']})" if m.get("slot") else ""), timestamp)
    for kind, key, label in (("keyboard_lost", "has_keyboard", "Keyboard"), ("mouse_lost", "has_mouse", "Mouse")):
        if prev["flags"].get(key) is True and flags.get(key) is False:
            log_incident(device_id, hostname, kind, f"{label} no longer detected", timestamp)
    return disk_alert

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
                if not get_maintenance(device_id):
                    log_incident(device_id, info.get("hostname") or device_id, "crashed",
                                 "Stopped reporting without a clean shutdown")
                await manager.broadcast({
                    "type": "status",
                    "data": {"device_id": device_id, "status": "crashed"}
                })

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(watch_for_crashes())
    asyncio.create_task(prune_loop())


# ----------------- AUTH (dashboard login -> signed token) -----------------
# Agents keep using the shared API_KEY (report / shutdown / report-issue).
# Everything the dashboard touches needs a login token instead, so the page
# no longer carries any secret in its source.
def _load_secret() -> bytes:
    if not SECRET_PATH.exists():
        SECRET_PATH.write_bytes(secrets.token_bytes(32))
    return SECRET_PATH.read_bytes()

TOKEN_SECRET = _load_secret()

def init_users():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY COLLATE NOCASE,
            salt TEXT,
            password_hash TEXT,
            created_at REAL
        )
    """)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "role" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'user'")
    if "pw_changed" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN pw_changed REAL DEFAULT 0")
    # There is exactly ONE admin: the built-in account. It always exists, and
    # every other account is a regular user (older installs may have had more).
    admin_name = DEFAULT_ADMIN[0]
    if conn.execute("SELECT COUNT(*) FROM users WHERE username = ?", (admin_name,)).fetchone()[0]:
        conn.execute("UPDATE users SET role = 'admin' WHERE username = ?", (admin_name,))
    else:
        now = time.time()
        salt = secrets.token_bytes(16)
        conn.execute("INSERT INTO users (username, salt, password_hash, created_at, role, pw_changed) VALUES (?, ?, ?, ?, 'admin', ?)",
                     (admin_name, salt.hex(), _hash_password(DEFAULT_ADMIN[1], salt), now, now))
    conn.execute("UPDATE users SET role = 'user' WHERE username != ?", (admin_name,))
    conn.commit()
    conn.close()


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000).hex()

def count_users() -> int:
    conn = sqlite3.connect(DB_PATH)
    n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    return n

def list_users(include_admin: bool = False) -> list:
    """The admin account is hidden from user management unless asked for."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT username, role, created_at FROM users ORDER BY username COLLATE NOCASE").fetchall()
    conn.close()
    return [{"username": r[0], "role": r[1] or "user", "created_at": r[2]}
            for r in rows if include_admin or (r[1] or "user") != "admin"]

def get_role(username: str):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT role FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    return (row[0] or "user") if row else None

def get_user_exact(username: str) -> str:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT username FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    return row[0] if row else username

def create_user(username: str, password: str, role: str = "user"):
    salt = secrets.token_bytes(16)
    now = time.time()
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("INSERT INTO users (username, salt, password_hash, created_at, role, pw_changed) VALUES (?, ?, ?, ?, ?, ?)",
                     (username, salt.hex(), _hash_password(password, salt), now, role, now))
        conn.commit()
    finally:
        conn.close()

def set_user_password(username: str, password: str) -> bool:
    """Also stamps pw_changed, which logs out every token issued before now."""
    salt = secrets.token_bytes(16)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("UPDATE users SET salt = ?, password_hash = ?, pw_changed = ? WHERE username = ?",
                       (salt.hex(), _hash_password(password, salt), time.time(), username))
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed

def delete_user(username: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("DELETE FROM users WHERE username = ?", (username,))
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed

def check_credentials(username: str, password: str):
    """Returns the stored username (original casing) if the login is valid, else None."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT username, salt, password_hash FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    if not row:
        _hash_password(password, b"x" * 16)  # keep timing similar for unknown users
        return None
    if hmac.compare_digest(_hash_password(password, bytes.fromhex(row[1])), row[2]):
        return row[0]
    return None

def uses_default_password(username: str) -> bool:
    """True while the built-in admin account still has its factory password."""
    return username.lower() == DEFAULT_ADMIN[0] and check_credentials(DEFAULT_ADMIN[0], DEFAULT_ADMIN[1]) is not None

def make_token(username: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"u": username, "iat": time.time(), "exp": time.time() + TOKEN_TTL_HOURS * 3600}).encode()).decode()
    sig = hmac.new(TOKEN_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"

def verify_token(token: str):
    """Returns the username for a valid, unexpired token, else None."""
    try:
        payload, sig = token.rsplit(".", 1)
        expected = hmac.new(TOKEN_SECRET, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
        if data["exp"] < time.time():
            return None
        # The account must still exist, and the token must be newer than the
        # last password change — so deleting a user or resetting a password
        # logs that person out immediately.
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT username, pw_changed FROM users WHERE username = ?", (data["u"],)).fetchone()
        conn.close()
        if not row or data.get("iat", 0) < (row[1] or 0):
            return None
        return row[0]
    except Exception:
        return None

def current_user(request: Request) -> str:
    """Use as a dependency (or call directly) on every dashboard-facing route."""
    header = request.headers.get("Authorization", "")
    user = verify_token(header[7:]) if header.startswith("Bearer ") else None
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    return user

def require_admin(request: Request) -> str:
    user = current_user(request)
    if get_role(user) != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user

# Throttle wrong passwords: 5 failures per minute per IP.
_login_failures: dict = {}

def _check_throttle(ip: str):
    now = time.time()
    recent = [t for t in _login_failures.get(ip, []) if now - t < 60]
    _login_failures[ip] = recent
    if len(recent) >= 5:
        raise HTTPException(status_code=429, detail="Too many failed attempts — wait a minute and try again")

def _record_failure(ip: str):
    _login_failures.setdefault(ip, []).append(time.time())

init_users()   # after the helpers above: it may seed the default admin, which needs password hashing

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
    previous_payload = _latest_payload(device_id) if device_id not in incident_state else None

    save_report(payload)

    device_status[device_id] = {"last_seen": payload["timestamp"], "status": "online", "hostname": hostname}
    payload["status"] = "online"

    # Auto-capture a baseline the very first time we ever see this device
    # (internal parts and other peripherals; keyboard/mouse have their own live check)
    combined = combined_components(payload)
    if get_baseline(device_id) is None and combined:
        save_baseline(device_id, combined)

    payload["missing_components"] = compute_missing(device_id, combined)
    # Prefer the agent's own computed flags (which may be pinned to a
    # specific device's VID/PID) over recomputing generically here — only
    # fall back for older agents that predate that feature.
    payload["peripheral_flags"] = payload.get("peripheral_flags") or compute_peripheral_flags(payload.get("devices", []))
    payload["under_maintenance"] = get_maintenance(device_id)
    payload["disk_low"] = disk_is_low(payload)
    disk_alert = check_incidents(device_id, hostname, previous_payload, payload["missing_components"],
                                 payload["peripheral_flags"], payload["timestamp"], disk_percent(payload))
    if disk_alert:
        disk_alert["disk_free_gb"] = (payload.get("hardware") or {}).get("disk_free_gb")
        await manager.broadcast({"type": "disk_alert", "data": disk_alert})

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
        device_status[device_id] = {**device_status.get(device_id, {}), "last_seen": time.time(), "status": "offline"}
        await manager.broadcast({
            "type": "status",
            "data": {"device_id": device_id, "status": "offline"}
        })
    return {"status": "ok"}

@app.post("/api/baseline/{device_id}/reset")
async def reset_baseline(device_id: str, request: Request):
    """Call this after an intentional hardware change, so the new configuration
    becomes the new 'expected' baseline instead of showing as missing forever."""
    current_user(request)

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
    name = require_admin(request)   # technicians record work by completing an assigned task instead

    body = await request.json()
    note = body.get("note", "").strip()
    if not note:
        raise HTTPException(status_code=400, detail="note is required")

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
    name = current_user(request)   # the logged-in account — never a name typed by the client

    body = await request.json()
    flag = bool(body.get("under_maintenance"))
    reason = body.get("reason", "").strip()
    note = body.get("note", "").strip()
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
        ticket_id = save_repair_ticket(device_id, hostname, reason, "", "", "", "", "Pending", name, timestamp, assigned_to=name, assigned_by=name)
        ticket = get_repair_ticket(ticket_id)
        await manager.broadcast({"type": "repair_ticket", "data": ticket})
    else:
        # Back in service: close this PC's open tickets (optional note appended).
        if body.get("complete_tickets", True):
            completed_ids = complete_open_tickets(device_id, note, name)
            for tid in completed_ids:
                await manager.broadcast({"type": "repair_ticket", "data": get_repair_ticket(tid)})

    await manager.broadcast({
        "type": "maintenance",
        "data": {"device_id": device_id, "hostname": hostname, "under_maintenance": flag, "set_by": name, "timestamp": timestamp},
    })
    return {"status": "ok", "ticket_id": ticket["id"] if ticket else None, "completed_ticket_ids": completed_ids}

@app.post("/api/repair-tickets/{device_id}")
async def create_repair_ticket(device_id: str, request: Request):
    """Admin-only: assign a repair task for a PC to a technician. The technician
    later starts it and completes it with what they did."""
    admin = require_admin(request)

    body = await request.json()
    reported_problem = body.get("reported_problem", "").strip()
    assigned_to = body.get("assigned_to", "").strip()

    if not reported_problem:
        raise HTTPException(status_code=400, detail="reported_problem is required")
    if not assigned_to:
        raise HTTPException(status_code=400, detail="assigned_to is required")
    techs = {u["username"] for u in list_users()}   # non-admin accounts only
    if assigned_to not in techs:
        raise HTTPException(status_code=400, detail="assigned_to must be an existing technician account")

    devices = get_latest_per_device()
    match = next((d for d in devices if d["device_id"] == device_id), None)
    hostname = match["hostname"] if match else device_id
    timestamp = time.time()

    ticket_id = save_repair_ticket(
        device_id, hostname, reported_problem, "", "", "", "",
        "Pending", assigned_to, timestamp, assigned_to=assigned_to, assigned_by=admin,
    )
    ticket = get_repair_ticket(ticket_id)
    await manager.broadcast({"type": "repair_ticket", "data": ticket})
    return {"status": "ok", "id": ticket_id}

@app.get("/api/repair-tickets")
async def list_repair_tickets(status: str = None, user: str = Depends(current_user)):
    """Fleet-wide ticket list, optionally filtered by status — used by the
    Repair Tracking page's "All PCs" view and report generation."""
    return get_repair_tickets(status=status)

@app.get("/api/repair-tickets/{device_id}")
async def list_repair_tickets_for_device(device_id: str, status: str = None, user: str = Depends(current_user)):
    """One device's maintenance history."""
    return get_repair_tickets(device_id=device_id, status=status)

@app.post("/api/repair-tickets/{ticket_id}/status")
async def update_repair_ticket_status_route(ticket_id: int, request: Request):
    """Only the technician the task is assigned to (or the admin) may update it.
    Completing requires a note of what was done; optional part details and an
    option to save the PC's current components as its new baseline."""
    user = current_user(request)

    body = await request.json()
    status = body.get("repair_status", "").strip()
    if status not in REPAIR_STATUSES:
        raise HTTPException(status_code=400, detail=f"repair_status must be one of {REPAIR_STATUSES}")

    ticket = get_repair_ticket(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    if get_role(user) != "admin" and ticket["assigned_to"] != user:
        raise HTTPException(status_code=403, detail="This task is assigned to someone else")

    def opt(k):
        v = body.get(k)
        return v.strip() if isinstance(v, str) else None
    actions = opt("actions_taken")
    if status == "Completed" and not (actions or ticket["actions_taken"]):
        raise HTTPException(status_code=400, detail="actions_taken is required to complete a task")

    # Parts taken from the inventory while completing: deducted first (all or nothing), so a
    # stock shortage stops the completion instead of leaving a half-saved task.
    used = None
    if body.get("inventory_items"):
        if status != "Completed":
            raise HTTPException(status_code=400, detail="Inventory parts can only be recorded when completing a task")
        if ticket["repair_status"] == "Completed":
            raise HTTPException(status_code=400, detail="This task is already completed")
        used = _take_stock(_parse_wanted(body.get("inventory_items")), user, ticket["device_id"], ticket["hostname"],
                           ticket_id, ticket["reported_problem"], "Used while completing the task")

    finish_repair_ticket(ticket_id, status, user, actions, opt("part_type"), opt("model_before"), opt("model_after"))
    if used:
        await manager.broadcast({"type": "inventory_changed", "data": {"used_on": ticket["hostname"], "by": user},
                                 "low_items": used["low_items"]})

    if status == "Completed":
        # Same PC history entry the old "Log a Repair" made, so history still shows it.
        done = actions or ticket["actions_taken"]
        ts = time.time()
        log_device_event(ticket["device_id"], ticket["hostname"], "repaired", {"type": user, "name": done}, ts)
        await manager.broadcast({"type": "device_event", "data": {
            "device_id": ticket["device_id"], "hostname": ticket["hostname"], "event_type": "repaired",
            "component_type": user, "component_name": done, "timestamp": ts}})
        if body.get("update_baseline"):
            match = next((d for d in get_latest_per_device() if d["device_id"] == ticket["device_id"]), None)
            if match:
                save_baseline(ticket["device_id"], combined_components(match))

    ticket = get_repair_ticket(ticket_id)
    if status == "Completed":
        log_part_replacement(ticket)
    await manager.broadcast({"type": "repair_ticket", "data": ticket})
    return {"status": "ok", "inventory": used}

@app.post("/api/repair-tickets/{ticket_id}/edit")
async def edit_repair_ticket(ticket_id: int, request: Request):
    """Admin-only: fix a mis-assigned task — change its PC, its description, or who it's
    assigned to. Changing the technician hands it to them fresh (Pending). A completed
    task can't be edited (delete it instead if it was a mistake)."""
    admin = require_admin(request)
    body = await request.json()

    ticket = get_repair_ticket(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    if ticket["repair_status"] == "Completed":
        raise HTTPException(status_code=400, detail="A completed task can't be edited")

    device_id = body.get("device_id", ticket["device_id"]).strip()
    problem = body.get("reported_problem", ticket["reported_problem"]).strip()
    new_tech = body.get("assigned_to", ticket["assigned_to"] or ticket["technician"]).strip()
    if not problem:
        raise HTTPException(status_code=400, detail="reported_problem is required")
    if new_tech not in {u["username"] for u in list_users()}:
        raise HTTPException(status_code=400, detail="assigned_to must be an existing technician account")

    match = next((d for d in get_latest_per_device() if d["device_id"] == device_id), None)
    hostname = match["hostname"] if match else (ticket["hostname"] if device_id == ticket["device_id"] else device_id)

    handed_over = new_tech != (ticket["assigned_to"] or ticket["technician"])
    conn = sqlite3.connect(DB_PATH)
    if handed_over:
        conn.execute("""UPDATE repair_tickets SET device_id = ?, hostname = ?, reported_problem = ?, assigned_to = ?, technician = ?,
                        assigned_by = ?, reassigned_from = ?, repair_status = 'Pending', timestamp = ? WHERE id = ?""",
                     (device_id, hostname, problem, new_tech, new_tech, admin, ticket["assigned_to"] or ticket["technician"], time.time(), ticket_id))
    else:
        conn.execute("UPDATE repair_tickets SET device_id = ?, hostname = ?, reported_problem = ? WHERE id = ?",
                     (device_id, hostname, problem, ticket_id))
    conn.commit()
    conn.close()
    ticket = get_repair_ticket(ticket_id)
    await manager.broadcast({"type": "repair_ticket", "data": ticket, "edited": not handed_over})
    return {"status": "ok"}

@app.delete("/api/repair-tickets/{ticket_id}")
async def delete_repair_ticket(ticket_id: int, request: Request):
    """Admin-only: pull a task back entirely (e.g. assigned by mistake)."""
    require_admin(request)
    ticket = get_repair_ticket(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM repair_tickets WHERE id = ?", (ticket_id,))
    conn.commit()
    conn.close()
    await manager.broadcast({"type": "repair_ticket_deleted", "data": ticket})
    return {"status": "ok"}

@app.get("/api/technicians")
async def list_technicians(request: Request):
    """Technician names for the admin's Reassign picker."""
    require_admin(request)
    return [u["username"] for u in list_users()]

@app.post("/api/repair-tickets/{ticket_id}/reassign")
async def reassign_repair_ticket(ticket_id: int, request: Request):
    """Admin-only: hand an unfinished task to another technician (e.g. when the
    assignee can't work). It goes back to Pending for the new person."""
    user = require_admin(request)
    body = await request.json()
    new_tech = body.get("assigned_to", "").strip()

    ticket = get_repair_ticket(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    if ticket["repair_status"] == "Completed":
        raise HTTPException(status_code=400, detail="A completed task can't be reassigned")
    if new_tech not in {u["username"] for u in list_users()}:
        raise HTTPException(status_code=400, detail="assigned_to must be an existing technician account")
    if new_tech == ticket["assigned_to"]:
        raise HTTPException(status_code=400, detail="Task is already assigned to that technician")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("""UPDATE repair_tickets SET assigned_to = ?, technician = ?, assigned_by = ?, reassigned_from = ?,
                    repair_status = 'Pending', timestamp = ? WHERE id = ?""",
                 (new_tech, new_tech, user, ticket["assigned_to"], time.time(), ticket_id))
    conn.commit()
    conn.close()
    ticket = get_repair_ticket(ticket_id)
    await manager.broadcast({"type": "repair_ticket", "data": ticket})
    return {"status": "ok"}

@app.get("/api/devices")
async def list_devices(user: str = Depends(current_user)):
    return get_latest_per_device()

@app.get("/api/history/{device_id}")
async def device_history(device_id: str, limit: int = 100, user: str = Depends(current_user)):
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
async def list_issues(include_resolved: bool = False, user: str = Depends(current_user)):
    return get_issues(include_resolved)

@app.post("/api/issues/{issue_id}/resolve")
async def resolve_issue_route(issue_id: int, request: Request):
    require_admin(request)   # reported problems are handled by the admin, who turns them into tasks
    resolve_issue(issue_id)
    await manager.broadcast({"type": "issue_resolved", "data": {"id": issue_id}})
    return {"status": "ok"}

@app.post("/api/login")
async def login(request: Request):
    ip = request.client.host if request.client else "?"
    _check_throttle(ip)
    body = await request.json()
    username = check_credentials(str(body.get("username", "")).strip(), str(body.get("password", "")))
    if not username:
        _record_failure(ip)
        raise HTTPException(status_code=401, detail="Wrong username or password")
    return {"token": make_token(username), "username": username, "role": get_role(username),
            "default_password": uses_default_password(username)}

@app.post("/api/auth/change-password")
async def change_own_password(request: Request, user: str = Depends(current_user)):
    """Admin only: change your own password (needs the current one).
    Technicians don't change passwords themselves — the admin resets theirs from the Users tab."""
    if get_role(user) != "admin":
        raise HTTPException(status_code=403, detail="Only the admin can change passwords")
    ip = request.client.host if request.client else "?"
    _check_throttle(ip)
    body = await request.json()
    if check_credentials(user, str(body.get("current_password", ""))) is None:
        _record_failure(ip)
        raise HTTPException(status_code=400, detail="Current password is wrong")
    new_password = str(body.get("new_password", ""))
    if len(new_password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=400, detail=f"New password must be at least {MIN_PASSWORD_LENGTH} characters")
    set_user_password(user, new_password)   # logs out every older token...
    return {"status": "ok", "token": make_token(user)}   # ...so hand back a fresh one for this browser

@app.get("/api/auth/me")
async def auth_me(user: str = Depends(current_user)):
    return {"username": user, "role": get_role(user), "default_password": uses_default_password(user)}

# ----- Admin: manage accounts -----
def _valid_new_password(body) -> str:
    password = str(body.get("password", ""))
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=400, detail=f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    return password

@app.get("/api/users")
async def admin_list_users(admin: str = Depends(require_admin)):
    return list_users()   # the admin account itself is not listed

@app.post("/api/users")
async def admin_create_user(request: Request, admin: str = Depends(require_admin)):
    body = await request.json()
    username = str(body.get("username", "")).strip()
    if not username:
        raise HTTPException(status_code=400, detail="username is required")
    if username.lower() == DEFAULT_ADMIN[0]:
        raise HTTPException(status_code=409, detail=f"'{username}' is reserved")
    password = _valid_new_password(body)
    try:
        create_user(username, password, "user")   # there is only one admin; new accounts are always users
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail=f"'{username}' already exists")
    return {"status": "ok"}

@app.post("/api/users/{username}/password")
async def admin_reset_password(username: str, request: Request, admin: str = Depends(require_admin)):
    body = await request.json()
    password = _valid_new_password(body)
    if not set_user_password(username, password):
        raise HTTPException(status_code=404, detail="No such user")
    # Resetting your own password invalidates your own token, so hand back a fresh one.
    if username.lower() == admin.lower():
        return {"status": "ok", "token": make_token(get_user_exact(username))}
    return {"status": "ok"}

@app.delete("/api/users/{username}")
async def admin_delete_user(username: str, admin: str = Depends(require_admin)):
    if username.lower() == admin.lower():
        raise HTTPException(status_code=400, detail="You can't delete your own account")
    role = get_role(username)
    if role is None:
        raise HTTPException(status_code=404, detail="No such user")
    if role == "admin":
        raise HTTPException(status_code=400, detail="The admin account can't be deleted")
    delete_user(username)
    return {"status": "ok"}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    if not verify_token(websocket.query_params.get("token", "")):
        await websocket.accept()
        await websocket.close(code=4401)   # accepted first so the browser can read the close code
        return
    await manager.connect(websocket)
    await websocket.send_json({"type": "snapshot", "data": get_latest_per_device()})
    await websocket.send_json({"type": "issues_snapshot", "data": get_issues()})
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# ----------------- INVENTORY (parts stock) -----------------
# Anyone logged in can add items, adjust stock, and take stock out for a PC (the stock list's
# tick boxes -> "Use on a PC", or while completing a task). Every change goes into the stock
# log with the price per unit at that moment, the cost, and the PC / task it went to.
# (The old parts-proposal feature was removed; its tables are left in the DB untouched.)

def _int_field(body: dict, key: str, minimum: int = 0, required: bool = True):
    v = body.get(key)
    if v is None or v == "":
        if required:
            raise HTTPException(status_code=400, detail=f"{key} is required")
        return None
    if isinstance(v, bool) or not (isinstance(v, int) or (isinstance(v, str) and v.strip().lstrip("-").isdigit())):
        raise HTTPException(status_code=400, detail=f"{key} must be a whole number")
    n = int(v)
    if n < minimum:
        raise HTTPException(status_code=400, detail=f"{key} must be at least {minimum}")
    if n > 1_000_000:
        raise HTTPException(status_code=400, detail=f"{key} is too large")
    return n

def _price_field(body: dict, key: str):
    """A peso amount, 0 or more, rounded to centavos. Empty -> None."""
    v = body.get(key)
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        raise HTTPException(status_code=400, detail=f"{key} must be a number")
    try:
        n = float(str(v).strip().replace(",", ""))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{key} must be a number")
    if n != n or n < 0:
        raise HTTPException(status_code=400, detail=f"{key} must be 0 or more")
    if n > 10_000_000:
        raise HTTPException(status_code=400, detail=f"{key} is too large")
    return round(n, 2)

ITEM_COLS = "id, name, category, quantity, unit, notes, created_by, updated_by, updated_at, unit_price, min_qty, board_platform, ram_type"

# Motherboards carry two extra details: Intel or AMD, and which RAM generation they take.
BOARD_PLATFORMS = ("Intel", "AMD")
RAM_TYPES = ("DDR2", "DDR3", "DDR4", "DDR5")

def is_motherboard(category: str) -> bool:
    return (category or "").strip().lower() in ("motherboard", "motherboards", "mobo", "mainboard")

def _board_fields(body: dict, category: str) -> tuple:
    """(board_platform, ram_type) for a motherboard - both required - else (None, None)."""
    if not is_motherboard(category):
        return None, None
    platform, ram = _clean(body, "board_platform", 10), _clean(body, "ram_type", 10)
    if platform not in BOARD_PLATFORMS:
        raise HTTPException(status_code=400, detail="Pick whether the motherboard is Intel or AMD")
    if ram not in RAM_TYPES:
        raise HTTPException(status_code=400, detail="Pick which RAM type the motherboard takes (DDR2-DDR5)")
    return platform, ram

def _item_row(r) -> dict:
    qty, min_qty = r[3] or 0, r[10] or 0
    return {"id": r[0], "name": r[1], "category": r[2] or "", "quantity": qty, "unit": "",   # units were dropped; old values are ignored
            "notes": r[5] or "", "created_by": r[6], "updated_by": r[7], "updated_at": r[8],
            "unit_price": round(r[9] or 0, 2), "min_qty": min_qty, "low": min_qty > 0 and qty <= min_qty,
            "board_platform": r[11] or "", "ram_type": r[12] or ""}

def get_item(item_id: int):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(f"SELECT {ITEM_COLS} FROM inventory_items WHERE id = ?", (item_id,)).fetchone()
    conn.close()
    return _item_row(row) if row else None

def _log_stock(conn, item_id: int, name: str, change: int, qty_after: int, reason: str, by: str,
               unit_price=None, device_id=None, hostname=None, ticket_id=None, task=None):
    cost = round(abs(change) * unit_price, 2) if unit_price is not None else None
    conn.execute(
        "INSERT INTO inventory_log (item_id, item_name, change, qty_after, reason, by_user, timestamp, "
        "unit_price, cost, device_id, hostname, ticket_id, task) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (item_id, name, change, qty_after, reason, by, time.time(), unit_price, cost, device_id, hostname, ticket_id, task))

def _became_low(min_qty: int, before: int, after: int) -> bool:
    """True when this change took the item down to (or below) its low-stock level, or emptied it."""
    return min_qty > 0 and after < before and (after <= min_qty < before or after == 0)

def _low_info(item: dict) -> dict:
    return {"name": item["name"], "quantity": item["quantity"], "unit": item["unit"], "min_qty": item["min_qty"]}

def _clean(body: dict, key: str, limit: int) -> str:
    v = body.get(key)
    return v.strip()[:limit] if isinstance(v, str) else ""

def _device_hostname(device_id: str):
    """The PC's name from its newest report (or from a task if it never reported)."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT hostname FROM reports WHERE device_id = ? ORDER BY id DESC LIMIT 1", (device_id,)).fetchone()
    if not row:
        row = conn.execute("SELECT hostname FROM repair_tickets WHERE device_id = ? ORDER BY id DESC LIMIT 1", (device_id,)).fetchone()
    conn.close()
    return row[0] if row else None

def _parse_wanted(raw) -> dict:
    """[{item_id, quantity}, ...] -> {item_id: total quantity}."""
    if not isinstance(raw, list) or not raw:
        raise HTTPException(status_code=400, detail="Tick at least one item")
    wanted = {}
    for it in raw:
        if not isinstance(it, dict):
            raise HTTPException(status_code=400, detail="Bad item")
        iid = _int_field(it, "item_id", 1)
        wanted[iid] = wanted.get(iid, 0) + _int_field(it, "quantity", 1)
    return wanted

def _take_stock(wanted: dict, user: str, device_id: str, hostname: str, ticket_id, task, reason: str) -> dict:
    """Take several items out of stock for one PC, all or nothing. Each line is logged with the
    price at this moment and its cost. Raises 400 (and changes nothing) if any item is short."""
    conn = sqlite3.connect(DB_PATH, isolation_level=None)   # we control the transaction ourselves
    lines, low = [], []
    try:
        conn.execute("BEGIN IMMEDIATE")   # lock out other writers so two people can't take the last one
        for iid, qty in wanted.items():
            row = conn.execute("SELECT name, quantity, unit_price, min_qty, unit FROM inventory_items WHERE id = ?", (iid,)).fetchone()
            if not row:
                raise HTTPException(status_code=400, detail="An item you picked no longer exists — reload and try again")
            name, have, price, min_qty, unit = row[0], row[1] or 0, round(row[2] or 0, 2), row[3] or 0, row[4] or ""
            if qty > have:
                raise HTTPException(status_code=400, detail=f"Only {have} of '{name}' in stock (you picked {qty})")
            after = have - qty
            conn.execute("UPDATE inventory_items SET quantity = ?, updated_by = ?, updated_at = ? WHERE id = ?",
                         (after, user, time.time(), iid))
            _log_stock(conn, iid, name, -qty, after, reason, user, price, device_id, hostname, ticket_id, task)
            lines.append({"item_id": iid, "name": name, "quantity": qty, "unit_price": price, "cost": round(qty * price, 2)})
            if _became_low(min_qty, have, after):
                low.append({"name": name, "quantity": after, "unit": "", "min_qty": min_qty})
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return {"items": lines, "total_cost": round(sum(l["cost"] for l in lines), 2), "low_items": low}

@app.get("/api/inventory")
async def list_inventory(user: str = Depends(current_user)):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(f"SELECT {ITEM_COLS} FROM inventory_items ORDER BY LOWER(name)").fetchall()
    conn.close()
    return [_item_row(r) for r in rows]

@app.post("/api/inventory")
async def add_inventory_item(request: Request):
    user = current_user(request)
    body = await request.json()
    name = _clean(body, "name", 120)
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    qty = _int_field(body, "quantity", 0, required=False) or 0
    price = _price_field(body, "unit_price") or 0
    min_qty = _int_field(body, "min_qty", 0, required=False) or 0
    category = _clean(body, "category", 60)
    platform, ram = _board_fields(body, category)
    conn = sqlite3.connect(DB_PATH)
    if conn.execute("SELECT 1 FROM inventory_items WHERE LOWER(name) = LOWER(?)", (name,)).fetchone():
        conn.close()
        raise HTTPException(status_code=409, detail=f"'{name}' is already in the inventory")
    cur = conn.execute(
        "INSERT INTO inventory_items (name, category, quantity, unit, notes, created_by, updated_by, updated_at, unit_price, min_qty, "
        "board_platform, ram_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (name, category, qty, "", _clean(body, "notes", 300), user, user, time.time(), price, min_qty, platform, ram))
    item_id = cur.lastrowid
    if qty:
        _log_stock(conn, item_id, name, qty, qty, "Added to inventory", user, price)
    conn.commit()
    conn.close()
    item = get_item(item_id)
    await manager.broadcast({"type": "inventory_changed", "data": item})
    return item

@app.post("/api/inventory/{item_id}/edit")
async def edit_inventory_item(item_id: int, request: Request):
    user = current_user(request)
    item = get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    body = await request.json()
    name = _clean(body, "name", 120) or item["name"]
    price = (_price_field(body, "unit_price") or 0) if "unit_price" in body else item["unit_price"]
    min_qty = (_int_field(body, "min_qty", 0, required=False) or 0) if "min_qty" in body else item["min_qty"]
    category = _clean(body, "category", 60) if "category" in body else item["category"]
    if (is_motherboard(category) and "board_platform" not in body and "ram_type" not in body
            and item["board_platform"] and item["ram_type"]):
        platform, ram = item["board_platform"], item["ram_type"]   # not being changed
    else:
        platform, ram = _board_fields(body, category)
    conn = sqlite3.connect(DB_PATH)
    if conn.execute("SELECT 1 FROM inventory_items WHERE LOWER(name) = LOWER(?) AND id != ?", (name, item_id)).fetchone():
        conn.close()
        raise HTTPException(status_code=409, detail=f"'{name}' is already in the inventory")
    conn.execute("UPDATE inventory_items SET name = ?, category = ?, unit = ?, notes = ?, unit_price = ?, min_qty = ?, "
                 "board_platform = ?, ram_type = ?, updated_by = ?, updated_at = ? WHERE id = ?",
                 (name, category,
                  "",
                  _clean(body, "notes", 300) if "notes" in body else item["notes"],
                  price, min_qty, platform, ram, user, time.time(), item_id))
    conn.commit()
    conn.close()
    item = get_item(item_id)
    await manager.broadcast({"type": "inventory_changed", "data": item})
    return item

@app.post("/api/inventory/{item_id}/stock")
async def adjust_inventory_stock(item_id: int, request: Request):
    """Change the stock by hand: send {"set": 10} for a new total or {"delta": -2} to add/remove.
    When adding, "unit_price" is what was paid per unit for this batch (defaults to the item's price)."""
    user = current_user(request)
    item = get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    body = await request.json()
    if body.get("set") not in (None, ""):
        new_qty = _int_field(body, "set", 0)
    elif body.get("delta") not in (None, ""):
        new_qty = item["quantity"] + _int_field(body, "delta", -1_000_000)
        if new_qty < 0:
            raise HTTPException(status_code=400, detail=f"Only {item['quantity']} in stock — can't remove more than that")
    else:
        raise HTTPException(status_code=400, detail="send either 'set' or 'delta'")
    change = new_qty - item["quantity"]
    if change == 0:
        return item
    reason = _clean(body, "reason", 200)
    if change < 0 and not reason:
        raise HTTPException(status_code=400, detail="Say why the stock is going down")
    reason = reason or "Restock"
    # Adding stock = a purchase: the price paid for this batch may differ from last time. It is
    # recorded on this log row (that's what counts as money spent) and becomes the item's price.
    price = item["unit_price"]
    if change > 0:
        batch = _price_field(body, "unit_price")
        if batch is not None:
            price = batch
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE inventory_items SET quantity = ?, unit_price = ?, updated_by = ?, updated_at = ? WHERE id = ?",
                 (new_qty, price, user, time.time(), item_id))
    _log_stock(conn, item_id, item["name"], change, new_qty, reason, user, price)
    conn.commit()
    conn.close()
    before = item["quantity"]
    item = get_item(item_id)
    low = [_low_info(item)] if _became_low(item["min_qty"], before, new_qty) else []
    await manager.broadcast({"type": "inventory_changed", "data": item, "low_items": low})
    return item

@app.post("/api/inventory/use")
async def use_inventory(request: Request):
    """Anyone logged in: take ticked items out of stock for one PC (optionally one of its tasks).
    Body: {"device_id": "...", "ticket_id": 12 (optional), "note": "...", "items": [{"item_id": 3, "quantity": 2}]}"""
    user = current_user(request)
    body = await request.json()
    device_id = _clean(body, "device_id", 200)
    if not device_id:
        raise HTTPException(status_code=400, detail="Pick the PC the parts are for")
    hostname = _device_hostname(device_id)
    if not hostname:
        raise HTTPException(status_code=400, detail="Unknown PC")
    ticket_id = _int_field(body, "ticket_id", 1, required=False)
    task = None
    if ticket_id:
        ticket = get_repair_ticket(ticket_id)
        if not ticket or ticket["device_id"] != device_id:
            raise HTTPException(status_code=400, detail="That task isn't for this PC")
        task = ticket["reported_problem"]
    wanted = _parse_wanted(body.get("items"))
    result = _take_stock(wanted, user, device_id, hostname, ticket_id, task, _clean(body, "note", 200) or "Used on PC")
    await manager.broadcast({"type": "inventory_changed", "data": {"used_on": hostname, "by": user},
                             "low_items": result["low_items"]})
    return {**result, "hostname": hostname}

@app.delete("/api/inventory/{item_id}")
async def delete_inventory_item(item_id: int, request: Request):
    require_admin(request)
    item = get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM inventory_items WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()
    await manager.broadcast({"type": "inventory_changed", "data": {"id": item_id, "deleted": True}})
    return {"status": "ok"}

LOG_COLS = "id, item_id, item_name, change, qty_after, reason, by_user, timestamp, unit_price, cost, device_id, hostname, ticket_id, task"

def _log_rows(kind: str = "", item_id: int = 0, since: float = 0, until: float = 0, limit=None) -> list:
    if kind not in ("", "deduction", "addition"):
        raise HTTPException(status_code=400, detail="kind must be deduction or addition")
    where, params = [], []
    if item_id:
        where.append("item_id = ?"); params.append(item_id)
    if since:
        where.append("timestamp >= ?"); params.append(since)
    if until:
        where.append("timestamp < ?"); params.append(until)
    if kind == "deduction":
        where.append("change < 0")
    elif kind == "addition":
        where.append("change > 0")
    sql = f"SELECT {LOG_COLS} FROM inventory_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC"
    if limit:
        sql += " LIMIT ?"; params.append(limit)
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [{"id": r[0], "item_id": r[1], "item_name": r[2], "change": r[3], "qty_after": r[4], "reason": r[5], "by": r[6],
             "timestamp": r[7], "unit_price": r[8], "cost": r[9], "device_id": r[10], "hostname": r[11],
             "ticket_id": r[12], "task": r[13]} for r in rows]

@app.get("/api/inventory/log")
async def inventory_log(item_id: int = 0, kind: str = "", since: float = 0, until: float = 0, limit: int = 500,
                        user: str = Depends(current_user)):
    """Stock changes, newest first. kind=deduction -> only stock taken out; kind=addition -> only stock put in.
    since / until = unix times; only changes in that window. Each row carries the price at the time and its cost
    (empty for changes made before prices were recorded)."""
    return _log_rows(kind, item_id, since, until, max(1, min(limit, 5000)))

def _peso(v) -> str:
    return "" if v is None else f"{v:.2f}"

@app.get("/api/inventory/export")
async def export_inventory(what: str = "stock", since: float = 0, until: float = 0, user: str = Depends(current_user)):
    """CSV download for Excel. what = stock | deductions | additions. The logs take an optional
    since / until (unix times) and are not capped like the on-screen list."""
    buf = io.StringIO()
    w = csv.writer(buf)
    if what == "stock":
        conn = sqlite3.connect(DB_PATH)
        items = [_item_row(r) for r in conn.execute(f"SELECT {ITEM_COLS} FROM inventory_items ORDER BY LOWER(name)").fetchall()]
        conn.close()
        w.writerow(["Item", "Category", "Board type", "RAM type", "In stock", "Price per unit (PHP)", "Stock value (PHP)",
                    "Low-stock level", "Low stock?", "Notes", "Last updated", "Updated by"])
        for i in items:
            w.writerow([i["name"], i["category"], i["board_platform"], i["ram_type"], i["quantity"], _peso(i["unit_price"]),
                        _peso(i["quantity"] * i["unit_price"]), i["min_qty"] or "", "YES" if i["low"] else "",
                        i["notes"], time.strftime("%Y-%m-%d %H:%M", time.localtime(i["updated_at"])) if i["updated_at"] else "",
                        i["updated_by"] or ""])
    elif what in ("deductions", "additions"):
        rows = _log_rows("deduction" if what == "deductions" else "addition", 0, since, until)
        money = what == "additions"   # spending is counted on additions only; deductions carry no prices
        w.writerow(["Date", "Time", "Item", "Quantity", "Left after"] + (["Price per unit (PHP)", "Cost (PHP)"] if money else [])
                   + ["PC", "Task", "Task ID", "By", "Reason / note"])
        for r in rows:
            t = time.localtime(r["timestamp"])
            w.writerow([time.strftime("%Y-%m-%d", t), time.strftime("%H:%M:%S", t), r["item_name"], abs(r["change"]), r["qty_after"]]
                       + ([_peso(r["unit_price"]), _peso(r["cost"])] if money else [])
                       + [r["hostname"] or "", r["task"] or "", r["ticket_id"] or "", r["by"], r["reason"] or ""])
    else:
        raise HTTPException(status_code=400, detail="what must be stock, deductions or additions")
    filename = f"inventory-{what}-{time.strftime('%Y-%m-%d')}.csv"
    return Response(content=("﻿" + buf.getvalue()).encode("utf-8"),   # BOM so Excel reads ₱ and accents right
                    media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})

# ----------------- ROOMS (filter buttons on Monitoring System) -----------------
# Each button shows the PCs whose ROOM (agent config.toml) contains `room` as a whole word/number:
# "201" matches ROOM = "201" and "Room 201", not "2012". Everyone sees them; only the admin edits.
def _rooms(conn) -> list:
    return [{"id": r[0], "label": r[1], "room": r[2]} for r in conn.execute("SELECT id, label, room FROM room_groups ORDER BY id")]

def _room_fields(body: dict) -> tuple:
    label, room = _clean(body, "label", 40), _clean(body, "room", 40)
    if not label:
        raise HTTPException(status_code=400, detail="Give the room a name")
    if not room:
        raise HTTPException(status_code=400, detail="Say which room it looks for (the ROOM in config.toml)")
    return label, room

@app.get("/api/rooms")
async def list_rooms(user: str = Depends(current_user)):
    conn = sqlite3.connect(DB_PATH)
    out = _rooms(conn)
    conn.close()
    return out

@app.post("/api/rooms")
async def add_room(request: Request):
    admin = require_admin(request)
    label, room = _room_fields(await request.json())
    conn = sqlite3.connect(DB_PATH)
    dup = conn.execute("SELECT label FROM room_groups WHERE LOWER(room) = LOWER(?) OR LOWER(label) = LOWER(?)", (room, label)).fetchone()
    if dup:
        conn.close()
        raise HTTPException(status_code=409, detail=f"Already a room button for that: '{dup[0]}'")
    conn.execute("INSERT INTO room_groups (label, room, created_by, created_at) VALUES (?, ?, ?, ?)", (label, room, admin, time.time()))
    conn.commit()
    out = _rooms(conn)
    conn.close()
    await manager.broadcast({"type": "rooms_changed", "data": out})
    return out

@app.post("/api/rooms/{room_id}/edit")
async def edit_room(room_id: int, request: Request):
    require_admin(request)
    label, room = _room_fields(await request.json())
    conn = sqlite3.connect(DB_PATH)
    if not conn.execute("SELECT 1 FROM room_groups WHERE id = ?", (room_id,)).fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Room not found")
    dup = conn.execute("SELECT label FROM room_groups WHERE (LOWER(room) = LOWER(?) OR LOWER(label) = LOWER(?)) AND id != ?",
                       (room, label, room_id)).fetchone()
    if dup:
        conn.close()
        raise HTTPException(status_code=409, detail=f"Already a room button for that: '{dup[0]}'")
    conn.execute("UPDATE room_groups SET label = ?, room = ? WHERE id = ?", (label, room, room_id))
    conn.commit()
    out = _rooms(conn)
    conn.close()
    await manager.broadcast({"type": "rooms_changed", "data": out})
    return out

@app.delete("/api/rooms/{room_id}")
async def delete_room(room_id: int, request: Request):
    require_admin(request)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM room_groups WHERE id = ?", (room_id,))
    conn.commit()
    out = _rooms(conn)
    conn.close()
    await manager.broadcast({"type": "rooms_changed", "data": out})
    return out

# ----------------- DASHBOARD (landing page: month graph + stock spending) -----------------
@app.get("/api/dashboard/month")
async def dashboard_month(since: float = 0, until: float = 0, user: str = Depends(current_user)):
    """Per-day counts for one month. since/until = the browser's local midnights (unix seconds)
    of the 1st of the month and the 1st of the next, so days line up with the viewer's calendar.
    Spending = money paid for stock that was ADDED (each batch at its own price)."""
    if not since or not until or until <= since or until - since > 32 * 86400:
        raise HTTPException(status_code=400, detail="since/until must span one month")
    days = max(1, round((until - since) / 86400))
    conn = sqlite3.connect(DB_PATH)

    def per_day(sql: str, params=()) -> list:
        out = [0] * days
        for ts, val in conn.execute(sql, (since, until, *params)).fetchall():
            i = int((ts - since) // 86400)
            if 0 <= i < days:
                out[i] += val if val is not None else 0
        return out

    assigned = per_day("SELECT timestamp, 1 FROM repair_tickets WHERE timestamp >= ? AND timestamp < ?")
    completed = per_day("SELECT completed_at, 1 FROM repair_tickets WHERE completed_at >= ? AND completed_at < ?")
    problems = per_day("SELECT timestamp, 1 FROM issues WHERE timestamp >= ? AND timestamp < ?")
    incidents = per_day("SELECT timestamp, 1 FROM incidents WHERE timestamp >= ? AND timestamp < ?")
    spending = [round(v, 2) for v in per_day("SELECT timestamp, cost FROM inventory_log WHERE timestamp >= ? AND timestamp < ? AND change > 0")]
    kinds = dict(conn.execute("SELECT kind, COUNT(*) FROM incidents WHERE timestamp >= ? AND timestamp < ? GROUP BY kind",
                              (since, until)).fetchall())
    spent_all = conn.execute("SELECT COALESCE(SUM(cost), 0) FROM inventory_log WHERE change > 0").fetchone()[0]
    purchases = conn.execute(
        "SELECT item_name, change, unit_price, cost, by_user, reason, timestamp FROM inventory_log "
        "WHERE change > 0 ORDER BY id DESC LIMIT 8").fetchall()
    conn.close()
    return {
        "days": days,
        "assigned": assigned, "completed": completed, "problems": problems, "incidents": incidents,
        "incident_kinds": {k: kinds.get(k, 0) for k in INCIDENT_KINDS},
        "spending": spending,
        "spent_month": round(sum(spending), 2),
        "spent_all_time": round(spent_all or 0, 2),
        "recent_purchases": [{"item_name": r[0], "quantity": r[1], "unit_price": r[2], "cost": r[3], "by": r[4],
                              "reason": r[5], "timestamp": r[6]} for r in purchases],
    }

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html_path = Path(__file__).parent / "dashboard.html"
    return FileResponse(html_path)