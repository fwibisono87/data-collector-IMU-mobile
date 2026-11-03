# Startup Guide

The system is **one process on one laptop** plus **one APK on each phone**:

- `master_backend/run.py` — FastAPI. Serves the phone WebSockets (`/ws/telemetry`,
  `/ws/control`, `/upload/csv`), the camera service (one ffmpeg per USB camera), the
  session/ZIP pipeline **and the dashboard** (static build) on port **8000**.
- `mobile_node/` — the native Kotlin app **IMU Node**. Nothing else runs on the phones.

There is no Node at runtime, no second web server, and no port 3000. `http://<laptop>:8000/`
is the dashboard.

Order of a normal day: laptop first (`start.bat`), then the phones (open the app), then
Setup → Start.

---

## 1. First-time setup on Windows (field laptop)

### 1.1 Python 3.12

Install **Python 3.12 (64-bit)** from <https://www.python.org/downloads/windows/> and tick
**"Add python.exe to PATH"** in the installer. Check in a new terminal:

```powershell
python --version        # Python 3.12.x
```

The launcher creates the virtual environment itself (`master_backend\.venv`) and installs
`master_backend\requirements.txt` — you do not need to do it by hand.

> If an older checkout has `master_backend\venv` next to it, delete it. The launcher and
> the docs use `master_backend\.venv` (the one that is git-ignored).

### 1.2 ffmpeg

The cameras are captured by ffmpeg on the backend, so `ffmpeg.exe` **and** `ffprobe.exe`
must be findable. The launcher checks this and refuses to start without them.

```powershell
mkdir vendor\ffmpeg\bin
# download https://www.gyan.dev/ffmpeg/builds/ -> ffmpeg-release-essentials.zip
# copy bin\ffmpeg.exe and bin\ffprobe.exe into vendor\ffmpeg\bin\
```

Accepted, in this order:

1. `IMU_FFMPEG` / `IMU_FFPROBE` in `master_backend\.env` (full paths);
2. `vendor\ffmpeg\bin\ffmpeg.exe`, then `vendor\ffmpeg\ffmpeg.exe`;
3. `PATH`.

Check the important part — a build **with** DirectShow (dshow) support, which every
Windows build of ffmpeg has:

```powershell
vendor\ffmpeg\bin\ffmpeg.exe -hide_banner -list_devices true -f dshow -i dummy
```

It prints the camera names and their `Alternative name @device_pnp_…`; those are what the
dashboard lists. (The alternative name is the stable key — two identical webcams share the
friendly name but not the alternative name.)

### 1.3 Environment file

```powershell
copy master_backend\.env.example master_backend\.env
```

Minimum to edit:

```env
SSD_PATH=D:/IMU_Data_SSD          # session folders + ZIPs + calibration references
RESCUE_PATH=D:/IMU_Data_Rescue    # rescue uploads pulled off phones
RECOVERY_PATH=D:/IMU_Data_Recovery
PORT=8000
BIND_HOST=0.0.0.0
LAN_SUBNET=192.168.1.0/24
FSYNC_INTERVAL_SEC=5
MAX_CONCURRENT_DEVICES=8
LATE_ACCEPT_SEC=600               # how long after STOP a phone may still deliver buffered data
SORT_CSV_ON_CLOSE=true            # repair out-of-order rows after a mid-session reconnect
```

Create the directories if they do not exist:

```powershell
mkdir D:\IMU_Data_SSD
mkdir D:\IMU_Data_Rescue
mkdir D:\IMU_Data_Recovery
```

`start_all.ps1` fails fast if one of the required keys is missing: a backend that silently
falls back to `./data` on `C:` is only discovered a session too late.

### 1.4 The dashboard build

The built dashboard is committed in `master_backend/app/static/` — the laptop needs no
Node. After a `git pull` that touched `dashboard/src/`, rebuild it **on a machine with
Node** and commit the output:

```bash
make dashboard        # cd dashboard && npm ci && npm run build
```

## 2. Daily start on Windows

Double-click **`start.bat`**, or in a terminal:

```powershell
.\start_all.ps1      # full: checks/repairs the venv and installs requirements
.\start.ps1          # daily: never touches pip (see below)
.\ops\ip.ps1         # print the LAN IP, dashboard URL and /health any time, read-only
```

What `start_all.ps1` does, in order:

1. validates `master_backend\.env` and exports its values (including `PORT` — `run.py`
   reads `PORT` *before* the app loads `.env`, so the launcher is what keeps listener and
   app in agreement);
2. stops a previous backend and any ffmpeg it left behind (a webcam can be opened by one
   process at a time);
3. finds `ffmpeg`/`ffprobe`, or fails with a download hint;
4. creates or repairs `master_backend\.venv` and installs `requirements.txt`
   (`start.ps1` skips this step — reinstalling dependencies minutes before a subject
   arrives is pure risk; run `start_all.ps1` after a `git pull` that changed
   `requirements.txt`);
5. starts `master_backend\run.py`, waits for `/health`, prints the LAN IP and the dashboard
   URL, and opens the dashboard in the default browser;
6. prints the phone instructions (below).

Stop everything with Ctrl+C in that window or `.\ops\stop.ps1` from another terminal; the
latter also kills the ffmpeg children (matched by this checkout, never a stranger's
ffmpeg).

**No auto-reload, anywhere.** `run.py` only reloads when `IMU_DEV_RELOAD=1`, which is a
development switch: a reload restarts the backend *under a running recording*. The operator
launchers force it to `0`; use `make dev` on a development machine only.

## 3. Installing the APK on the phones

Build it (Linux/macOS with the Android SDK; `make apk`):

```bash
cd mobile_node && ./gradlew assembleRelease
# -> mobile_node/app/build/outputs/apk/release/app-release.apk
```

**Release key (create it once, then back it up).** A release APK must always be signed
with the same dedicated key, otherwise Android refuses `adb install -r` and the only way
past it is an uninstall — which deletes the phone-local rescue CSVs. Create the key once:

```bash
cd mobile_node
keytool -genkeypair -v -keystore imu-release.jks -alias imu \
        -keyalg RSA -keysize 4096 -validity 36500
```

Then copy `mobile_node/keystore.properties.example` to `mobile_node/keystore.properties`
(both the file and `*.jks` are gitignored) and fill it in:

```properties
storeFile=imu-release.jks        # relative to mobile_node/, or an absolute path
storePassword=…
keyAlias=imu
keyPassword=…
```

For a one-off build the same values can come from the environment instead:
`IMU_KEYSTORE_FILE`, `IMU_KEYSTORE_PASSWORD`, `IMU_KEY_ALIAS`, `IMU_KEY_PASSWORD`.
Back up `imu-release.jks` and its passwords somewhere safe, never in git.

With no key configured the build still succeeds, but prints
`release APK is signed with this machine's DEBUG key — NOT FOR FIELD PHONES` and the
app version ends in `-debugsigned` (visible on the dashboard). Do not deploy such an APK.

**Old app on the phones.** The app id is now `id.ac.ui.cs.csl.fams.sapper`; the old build
was `com.example.sensors_app`. The new app installs **alongside** the old one, so no uninstall
is needed and nothing is lost. It gets a fresh `device_id` (the phone appears as a new device
in the study) and its per-phone MIUI settings must be applied to the new app. Remove the old
app only after pulling its rescue data:

```bash
adb -s <serial> pull /sdcard/Android/data/com.example.sensors_app/files/imu_sessions ./local_backup_<serial>
adb -s <serial> uninstall com.example.sensors_app
```

Every later build signed with the same release key upgrades normally with `adb install -r`.

Install on every connected phone **without losing data**:

```bash
./ops/install_phones.sh          # or: make install-phones
```

The script never uninstalls: the phone-local rescue CSVs live in app-specific external
storage, which Android deletes on uninstall. It reports the version it installed, and if a
signature mismatch makes `adb install -r` impossible it stops and tells you how to pull the
data off first. Manual equivalent:

```bash
adb install -r mobile_node/app/build/outputs/apk/release/app-release.apk
adb shell dumpsys package id.ac.ui.cs.csl.fams.sapper | grep versionName   # confirm
```

Then, on each phone: open **IMU Node**, pick a role, and let it connect. Discovery is mDNS
(`_imu-telemetry._tcp`) — if the laptop is not found, type the IP printed by the launcher.
The version each phone reports is shown on its card in Setup; all phones in one study must
be on the same version.

Per-phone MIUI/HyperOS settings (autostart, battery, notifications) are in
[`docs/REDMI_NOTE_12_SETUP.md`](docs/REDMI_NOTE_12_SETUP.md) — do them once per phone, or the OS
will kill the recording service mid-session.

## 4. Linux development setup

```bash
python3 -m venv master_backend/.venv
master_backend/.venv/bin/pip install -r master_backend/requirements.txt
apt install ffmpeg                     # or put the binaries in vendor/ffmpeg/bin/

cp master_backend/.env.example master_backend/.env   # SSD_PATH=./data is fine here
./start_all.sh                         # backend + dashboard on :8000, opens a browser
```

Without real webcams, use synthetic cameras — the whole pipeline (capture, recording,
calibration, ZIP) runs unchanged:

```bash
IMU_CAMERAS=fake IMU_FAKE_CAMERAS=4 ./start_all.sh
```

Tests and builds:

```bash
make test          # backend fast suite (pytest -m "not slow") + dashboard npm test
make test-slow     # camera/video/soak suites (minutes, real ffmpeg children)
make dashboard     # rebuild master_backend/app/static/ (commit the output)
make apk           # phone APK
```

For a phone over USB without Wi-Fi:

```bash
adb reverse tcp:8000 tcp:8000     # the phone then reaches the laptop on 127.0.0.1:8000
```

## 5. Environment variables

The launcher exports everything in `master_backend\.env`; anything you set in the
environment yourself wins over the file (python-dotenv does not override).

| Variable | Default | Meaning |
|----------|---------|---------|
| `PORT` | `8000` | The single port: dashboard, `/api/*`, `/ws/*`, `/health`. Must match `PORT` in `.env` — the launcher guarantees it. |
| `SSD_PATH` | `./data` | Root of all research data: `Data_Riset_IMU/<subject>_<tag>/<sid>_…`, the session ZIPs, `_state/` (notices, draft, session ledger) and `_calibration/`. Point it at the SSD. |
| `RESCUE_PATH` | `./data_rescue` | Where rescued/late data pulled off phones is staged. |
| `RECOVERY_PATH` | `./data_recovery` | Staging area for the legacy recovery upload path. |
| `IMU_CAMERAS` | (real discovery) | `fake` → synthetic `lavfi testsrc2` cameras; `stub` → in-memory test double, no ffmpeg; `off` → no cameras. |
| `IMU_FAKE_CAMERAS` | `2` | How many fake cameras (`fake:0`…`fake:n-1`). |
| `IMU_VIDEO_CODEC` | `vp9` | `vp9` or `vp8`. Read at every recording start. VP9 sustains 5×720p30 at ~37 % CPU; VP8 does not keep up on the current laptop (see `master_backend/app/camera/README.md`). |
| `IMU_FFMPEG` / `IMU_FFPROBE` | `vendor/ffmpeg/`, then `PATH` | Explicit binary paths when ffmpeg is not vendored. |
| `IMU_CORS_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | Comma-separated browser origins allowed to call the API. The packaged dashboard is served same-origin and needs none; these are the Vite dev server addresses. |
| `IMU_CAM_WIDTH` / `IMU_CAM_HEIGHT` / `IMU_CAM_FPS` | `1280` / `720` / `30` | Capture geometry requested from the cameras. |
| `FSYNC_INTERVAL_SEC` | `5` | CSV fsync interval during recording. |
| `LATE_ACCEPT_SEC` | `600` | How long after STOP late phone data is still merged. |
| `SORT_CSV_ON_CLOSE` | `true` | Sort rows by timestamp when closing (repairs a mid-session reconnect). |
| `MAX_CONCURRENT_DEVICES` | `8` | Phone connection cap. |
| `BIND_HOST`, `LAN_SUBNET` | `0.0.0.0`, … | Reserved/legacy: `run.py` always binds `0.0.0.0`. Kept so existing `.env` files stay valid. |
| `IMU_DEV_RELOAD` | unset | Development only: `1` restarts the backend on code changes. Never on the field laptop. |

## 6. Endpoints (for debugging)

| Path | Who uses it |
|------|-------------|
| `http://<laptop>:8000/` | Dashboard (static build, same origin as the API) |
| `GET /health` | Version/build id, session state, online phones, LAN IP |
| `GET /api/state`, `POST /api/session/{start,stop,label}` | Dashboard REST |
| `WS /ws/ui` | Dashboard live updates (snapshots, live samples, finalize progress) |
| `WS /ws/telemetry`, `WS /ws/control` | Phones (protobuf) |
| `POST /upload/csv` | Phone rescue upload |

`/health` reporting `build_id` is the same sha the dashboard shows in its top bar; if they
differ the browser is running an older bundle.

## 7. Common issues

| Problem | Fix |
|---------|-----|
| `FAIL: ffmpeg / ffprobe not found` | See §1.2. The dashboard shows no cameras at all until this is fixed. |
| Port 8000 already in use | `.\ops\stop.ps1` (kills the backend and its ffmpeg), then start again. If the owner is not this backend, the script says so and leaves it alone. |
| `ModuleNotFoundError` on backend start | Run from the repo root — or just use `start_all.ps1`, which sets it up. If it persists: delete `master_backend\.venv` and re-run `start_all.ps1`. |
| Cameras listed but no preview | A webcam can only be opened by one process: close Teams/Zoom/Camera, run `.\ops\stop.ps1`, start again. |
| Phones cannot find the laptop (mDNS) | Type the IP printed by the launcher in the app. Allow `python.exe` through Windows Firewall on the **Private** network (inbound TCP 8000, UDP 5353). |
| Phone disconnects mid-session (Redmi/Xiaomi) | [`docs/REDMI_NOTE_12_SETUP.md`](docs/REDMI_NOTE_12_SETUP.md) — autostart, battery "No restrictions", lock in recents, Wi-Fi on during sleep. Data is buffered and re-sent, not lost. |
| `SSD_PATH` write error on first run | Create the directory or fix the path in `.env`. Readiness shows the backend's own disk check as a blocker. |
| Dashboard shows `build …` different from `/health` | The static build is stale: `make dashboard` on a Node machine, commit `master_backend/app/static/`, `git pull` on the laptop. |

---

## 8. What the phone alarm means

If a phone drops mid-recording it loops an audible alarm + vibration and shows a red
**SILENCE** button; the dashboard shows that phone as `OFFLINE mm:ss` with the standing
guidance *"data is buffering on the phone — do not stop yet"*. Nothing is lost: the phone
keeps writing its rescue CSV the whole time and replays the buffered packets when the
network returns (within `LATE_ACCEPT_SEC`). Treat the alarm as **"go and look at the
phone"**, not as data loss. If it comes back after STOP, the upload appears as a row in the
Done view ("rescue upload … verified ✓") and the ZIP is rebuilt automatically.

---

## See also

- [`docs/DEPLOY.md`](docs/DEPLOY.md) — the before-recording-day checklist.
- [`docs/FIELD_CHECKLIST.md`](docs/FIELD_CHECKLIST.md) — the in-the-field checklist.
- [`docs/REDMI_NOTE_12_SETUP.md`](docs/REDMI_NOTE_12_SETUP.md) — per-phone OS settings.
- [`master_backend/ARCHITECTURE.md`](master_backend/ARCHITECTURE.md) — what the backend
  does between START and ZIP.
- [`docs/rebuild/SPEC.md`](docs/rebuild/SPEC.md) — why the system looks like this.