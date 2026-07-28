# Startup Guide — Backend & Frontend

## Prerequisites

- Python 3.10+
- Node.js 18+ / npm
- Windows (paths use `D:/` — adjust if different)

---

## Quick start (all-in-one)

Assumes env + build already done (see first-time setup below if not):

- Double-click **`start.bat`**, or run **`.\start.ps1`**, or **`make start`**.
- It prints the **Backend LAN IP** to type into the phones, then launches backend +
  frontend in separate windows and opens the dashboard.

To check the backend IP anytime WITHOUT stopping it, run **`.\ops\ip.ps1`** (or `make ip`)
in a second terminal.

### For peers pulling this repo

1. Check out this branch: `git fetch && git checkout experiment/operational-frontend-fixes && git pull`.
2. You need your own `master_backend/.env` (copy from `.env.example`), `master_backend/venv/`,
   and `master_frontend/node_modules/` — none of these travel with git. `start.ps1` warns if
   any are missing instead of failing outright.
3. The phones do **not** update from git — Flutter changes need a fresh
   `flutter build apk --release` and reinstall per phone.

See `connectivity_ops_fixes_plan.md` §9a for the full peer-onboarding rationale.

---

## Backend (FastAPI)

### 1. First-time setup

```powershell
cd master_backend
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment

```powershell
copy .env.example .env
```

Edit `.env` — minimum required:

```env
SSD_PATH=D:/IMU_Data_SSD        # where session CSVs are written
RESCUE_PATH=D:/IMU_Data_Rescue  # fallback if SSD disconnects
BIND_HOST=0.0.0.0
PORT=8000
LAN_SUBNET=192.168.1.0/24       # restrict to your local network
FSYNC_INTERVAL_SEC=5
MAX_CONCURRENT_DEVICES=8
LATE_ACCEPT_SEC=600             # how long after STOP a phone can still deliver buffered data
SORT_CSV_ON_CLOSE=true          # repair out-of-order rows from a mid-session reconnect
```

Create the storage directories if they don't exist:

```powershell
mkdir D:\IMU_Data_SSD
mkdir D:\IMU_Data_Rescue
```

### 3. Run

From the **repo root** (not inside `master_backend/`):

```powershell
# activate venv first
master_backend\venv\Scripts\activate

# Option A — dev runner with auto-reload
python master_backend/run.py

# Option B — direct uvicorn
uvicorn master_backend.app.main:app --host 0.0.0.0 --port 8000 --reload
```

Backend is ready when you see:
```
INFO:     Uvicorn running on http://0.0.0.0:8000
```

WebSocket endpoints:
- `ws://<laptop-ip>:8000/ws/telemetry` — sensor data stream (Flutter)
- `ws://<laptop-ip>:8000/ws/control` — command channel (Frontend ↔ Flutter)

---

## Frontend (Next.js)

### 1. First-time setup

```powershell
cd master_frontend
npm install
```

### 2. Run

```powershell
npm run dev
```

Open [http://localhost:3000](http://localhost:3000) in your browser.

For production build:

```powershell
npm run build
npm run start
```

---

## Typical startup order

1. Start **backend** first — it advertises itself via mDNS so Flutter can discover it.
2. Start **frontend** — connects to backend WebSocket automatically.
3. Open the Flutter app on each phone — it scans mDNS or enter the laptop IP manually.
4. Follow the **Preflight checklist** in the UI before starting any recording session.

---

## Finding your laptop IP (for Flutter manual pairing)

Run `.\ops\ip.ps1` — it lists every IPv4, marks the LAN one to use, checks whether the
backend is up on :8000, and probes /health. (Fallback: `ipconfig | findstr "IPv4"`.)

Use the IP on the same Wi-Fi subnet as the phones (e.g., `192.168.1.x`).

---

## Common issues

| Problem | Fix |
|---------|-----|
| `ModuleNotFoundError` on backend start | Run from repo root, not from inside `master_backend/` |
| Port 8000 already in use | `netstat -ano \| findstr :8000` → kill the PID (or `.\ops\stop.ps1`) |
| Frontend can't reach backend | Dashboard now shows the reason + a checklist on a failed connect; run `.\ops\ip.ps1` to confirm the backend IP and that :8000 is listening. Check the Windows Firewall inbound rule for TCP 8000 on the Private network. |
| Flutter can't find server via mDNS | Enter the laptop IP manually (the phone now shows a specific failure reason); if it drops mid-session on Xiaomi/Redmi, see `docs/REDMI_NOTE_12_SETUP.md` |
| Phone disconnects mid-session (Redmi/Xiaomi) | Apply `docs/REDMI_NOTE_12_SETUP.md` per phone (Autostart, Battery = No restrictions, lock in recents, keep Wi-Fi on during sleep). Data is buffered + re-flushed, not lost. |
| `SSD_PATH` write error on first run | Create the directory or update `.env` to an existing writable path |

---

## What the connectivity alarm means

If a phone drops mid-recording, it loops an audible alarm + vibration + a red
"DISCONNECTED · 00:24 · N packets buffered" banner until it reconnects or the operator
taps **SILENCE**. This is not a failure — the phone keeps recording locally the whole
time (see `docs/REDMI_NOTE_12_SETUP.md` → Data recovery) and buffers for replay once the
network returns. It is a **prompt to go check the phone**, not a data-loss alarm. The
dashboard mirrors the same signal (a beep + a permanent red banner while any device is
offline during RECORDING) plus a connectivity event log, so after the session you can see
exactly when and how long each drop lasted instead of guessing why a report is PARTIAL.

---

## See also

- `docs/REDMI_NOTE_12_SETUP.md` — per-phone MIUI/HyperOS setup to stop mid-session drops.
- `connectivity_ops_fixes_plan.md` — what changed in the launcher/diagnostics/Redmi fixes and why.
