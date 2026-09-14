# Windows Device Tracker

Live monitoring of multiple Windows PCs, viewable on a web dashboard from any browser.

## Structure
- `agent/agent_windows.py` — runs on each Windows PC, sends stats + device list every 5s
- `server/server.py` — runs on your central machine (Pi/VPS/home server), stores data + serves dashboard
- `server/dashboard.html` — live view, auto-updates via WebSocket

## 1. Set up the server (on your Pi/VPS)
```bash
cd server
pip install -r requirements.txt
```
Edit `API_KEY` in `server.py` (pick your own secret string).

Run it:
```bash
uvicorn server:app --host 0.0.0.0 --port 8000
```
Open `http://YOUR_SERVER_IP:8000` in a browser — dashboard starts empty until agents report in.

## 2. Set up the agent (on each Windows PC)
```powershell
cd agent
pip install -r requirements.txt
```
Edit the top of `agent_windows.py`:
- `SERVER_URL` → `http://YOUR_SERVER_IP:8000/api/report`
- `API_KEY` → same value as the server

Run it:
```powershell
python agent_windows.py
```

You should see `[OK] Reported at HH:MM:SS` every 5 seconds, and that PC will appear as a card on the dashboard within a few seconds.

## 3. Run the agent automatically on boot (optional)
Use Windows **Task Scheduler**:
- Trigger: "At log on"
- Action: `pythonw.exe C:\path\to\agent_windows.py` (use `pythonw` instead of `python` to avoid a console window)

## Notes
- `get_custom_data()` in the agent is a placeholder — wire it up to whatever app-specific data you want to report.
- SQLite (`reports.db`) stores full history; the dashboard only shows the latest report per device. Query the DB directly if you want historical charts later.
- The API key is a simple shared secret — fine for a home/LAN setup. If exposing the server to the public internet, put it behind HTTPS (e.g. via a reverse proxy like Caddy or nginx) and consider per-device keys.
