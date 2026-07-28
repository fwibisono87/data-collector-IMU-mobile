# Startup Guide

This guide covers the standard setup for the three runtime parts of the system:

- Backend: FastAPI service in `master_backend/`
- Operator dashboard: Next.js app in `master_frontend/`
- Mobile client: Flutter app in `mobile_node/`

The recommended order is backend first, then dashboard, then the phones.

## Prerequisites

- Python 3.10+
- Node.js 18+
- Flutter 3.3+ if you are building or running the mobile client
- `protoc` if you need to regenerate shared protobuf bindings
- Windows with a writable local storage path for session files

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

## 1. Backend setup

The backend is responsible for session state, WebSocket routing, audit logging, and mDNS discovery.

### First-time setup

```powershell
cd master_backend
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

### Configure environment

Create the backend environment file:

```powershell
copy .env.example .env
```

Minimum expected values:

```env
SSD_PATH=D:/IMU_Data_SSD
RESCUE_PATH=D:/IMU_Data_Rescue
BIND_HOST=0.0.0.0
PORT=8000
LAN_SUBNET=192.168.1.0/24
FSYNC_INTERVAL_SEC=5
MAX_CONCURRENT_DEVICES=8
LATE_ACCEPT_SEC=600             # how long after STOP a phone can still deliver buffered data
SORT_CSV_ON_CLOSE=true          # repair out-of-order rows from a mid-session reconnect
```

Create the storage directories if they do not already exist:

```powershell
mkdir D:\IMU_Data_SSD
mkdir D:\IMU_Data_Rescue
```

### Run the backend

Run this from the repository root, not from inside `master_backend/`:

```powershell
master_backend\venv\Scripts\activate
python master_backend/run.py
```

If you prefer to call uvicorn directly:

```powershell
uvicorn master_backend.app.main:app --host 0.0.0.0 --port 8000 --reload
```

The backend is ready when it logs that uvicorn is running on port 8000.

### Backend endpoints

- `ws://<laptop-ip>:8000/ws/telemetry` - sensor stream from the mobile client
- `ws://<laptop-ip>:8000/ws/control` - command channel between dashboard and mobile client
- `ws://<laptop-ip>:8000/ws/frontend` - dashboard connection for session state
- `ws://<laptop-ip>:8000/ws/live` - live chart updates for the dashboard

## 2. Frontend setup

The frontend is the operator dashboard. It handles connection, preflight checks, labeling, integrity review, and camera coordination.

### First-time setup

```powershell
cd master_frontend
npm install
```

### Run the dashboard

```powershell
npm run dev
```

Open http://localhost:3000 in your browser.

### Production build

```powershell
npm run build
npm run start
```

## 3. Mobile client setup

The Flutter app reads device sensors and connects to the backend over the local network.

### First-time setup

If you are building the app locally, install Flutter dependencies in `mobile_node/` and make sure the generated protobuf files are present.

### Connection notes

- The app can discover the backend via mDNS when both devices are on the same LAN.
- If discovery fails, enter the laptop IP manually.
- The dashboard preflight should pass before starting a recording session.

## Typical startup order

1. Start the backend first so it can register mDNS and accept WebSocket connections.
2. Start the frontend second so the operator can confirm the session state and preflight checks.
3. Open the Flutter app on each phone and connect it to the backend.
4. Verify the dashboard is ready before starting the recording session.

## Finding the laptop IP

Run `.\ops\ip.ps1` — it lists every IPv4, marks the LAN one to use, checks whether the
backend is up on :8000, and probes /health. (Fallback: `ipconfig | findstr "IPv4"`.)

Use the IP that is on the same Wi-Fi subnet as the phones, usually something like 192.168.1.x.

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

## Optional helper commands

The root `Makefile` includes a few shortcuts:

- `make proto` - regenerate Dart and Python protobuf bindings
- `make run-backend` - start the backend
- `make run-frontend` - start the dashboard
- `make install-backend` - install backend dependencies
- `make install-frontend` - install frontend dependencies

## See also

- `docs/REDMI_NOTE_12_SETUP.md` — per-phone MIUI/HyperOS setup to stop mid-session drops.
- `connectivity_ops_fixes_plan.md` — what changed in the launcher/diagnostics/Redmi fixes and why.
