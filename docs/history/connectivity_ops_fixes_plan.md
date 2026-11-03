> **Historical — describes the pre-v3 system; see [docs/rebuild/SPEC.md](../rebuild/SPEC.md).**

# Connectivity, Startup & Mid-Session Disconnect — Analysis & Surgical Fix Plan

**Author:** Opus (senior reviewer / analysis)
**Engineer:** Sonnet (implementation)
**Date:** 2026-07-23
**Branch:** `experiment/operational-frontend-fixes` (stay here — do NOT touch `master`/`main`).
**Status:** Ready for implementation. **Analysis + plan only — no code has been changed.**

---

## 0. TL;DR (read this first)

Three peer-reported operational problems. None of them is a data-integrity bug — the
recording pipeline, buffering, reconnect, and dedup all already work. These are **operator
ergonomics + one OEM power-management reality**. Every fix below is **additive** and does
**not** alter any working code path (connect handshake, recording, proto/WS contract,
reconnect loop, device lifecycle).

| # | Peer complaint (verbatim intent) | Verdict | Where the fix lives |
|---|----------------------------------|---------|---------------------|
| **1** | "add details kalau gabisa connect ws-nya, dan kasi cara gampang cek config IP backend (sekarang harus stop program dulu)" | **Real ergonomics gap.** Connect failures give a one-line dead-end message; there is no way to see the backend's actual LAN IP without stopping it and reading `.env`. | `master_frontend` (diagnostic), `master_backend` (banner + `/health`), new **`ops/`** CLI helper, `mobile_node` connect error text |
| **2** | "minta Makefile yang all-in-one (asumsi env udah ada, frontend udah dibuild, tinggal start aja)" | **Reasonable, but `make` is not installed on the Windows host** → deliver a Windows-native launcher AND a Makefile target that calls it. | New `start.ps1` / `start.bat` / `ops/` scripts + `Makefile` targets |
| **3** | "di pertengahan kadang HP-nya disconnect — app reason atau phone reason? (Redmi Note 12)" | **Predominantly PHONE reason** — MIUI/HyperOS power management on the Redmi Note 12 kills/suspends the app or its Wi-Fi when the screen is off. **The app already survives it** (buffers to disk, auto-reconnects, dedup makes re-send idempotent). The app can *reduce* the frequency with two additive levers it currently never uses. The *guaranteed* fix is a per-device OS setup checklist. | `mobile_node` (battery-opt + notification permission requests), new `docs/REDMI_NOTE_12_SETUP.md` |

**The critical honest answer to the peer's question (#3):** it is *both*, but weighted
~80% phone / 20% app-hygiene. The Redmi Note 12 (MIUI 14 / HyperOS, Android 13) is a
textbook "aggressive OEM" device. The app never requests the battery-optimization
exemption nor the Android-13 notification permission, so MIUI is free to freeze it. Fix
those two (app) **and** apply the per-device settings (OS), and the mid-session drops go
away. Data was never lost either way — it was buffered and re-flushed.

---

## 1. How the system actually works today (so we don't break it)

```
 mobile_node (Flutter, Redmi/Android phones — strapped to subject)
   connection_screen → enter Backend IP + role → WebSocketClient.connect(ip)
   control_ws   : DeviceRegister, then PING every 1 s, PONG timeout 8 s        (websocket_client.dart:330-343)
   telemetry_ws : SensorPacket binary stream @100 Hz                           (websocket_client.dart:98-110)
   on drop      : _onControlDisconnect → offline → buffer to disk → reconnect  (websocket_client.dart:267-302)
   FGS          : flutter_foreground_task, allowWakeLock+allowWifiLock=true    (foreground_service_handler.dart:27-33)

 master_backend (FastAPI on the laptop)
   run.py       : uvicorn host 0.0.0.0 port 8000 --reload                      (run.py:8-14)
   /ws/telemetry, /ws/control (mobile) ; /ws/frontend, /ws/live (dashboard)    (ws_handler.py)
   /health, /session (HTTP)                                                    (main.py:67-94)
   mDNS advertises real LAN IP (but only to logs)                              (main.py:126-164)

 master_frontend (Next.js operator dashboard on the laptop's browser)
   connect screen: enter Backend IP → wsClient.connect(ip)                     (page.tsx:211-247)
   auto-reconnect from localStorage.backendIp on mount                         (page.tsx:103-124)
   ws_client.ts : /ws/frontend (JSON) + /ws/live (chart), 3 s auto-retry       (ws_client.ts:112-141)
```

**Key facts that shape the fixes:**

- **Backend binds `0.0.0.0` but the phones/dashboard need the concrete LAN IP** (e.g.
  `192.168.1.100`). The backend *knows* this IP — `main._get_local_ip()` (`main.py:158-164`)
  and the mDNS block (`main.py:132-143`) compute it — but it is only written to logs, never
  surfaced in `/health` or a clear startup banner. That is the root of "harus stop program
  dulu untuk cek IP".
- **`make` is not on PATH** on this host (verified: only `mingw32-make.exe` in msys2 exists;
  `Get-Command make` returns nothing). Node, npm, and Python 3.11 are present.
- **Frontend `.next` exists but has no `BUILD_ID`** → it is a *dev* compile, not a production
  build. The all-in-one launcher must handle both ("start prod if built, else dev").
- **The app is defensive already**: silent drops are detected (`onDone`/`onError` on both
  sockets), data is buffered (`FallbackBufferManager`), reconnect retries every 3 s, and the
  backend dedups on `(device_id, session_id, sequence_number)` so re-flush is idempotent.
  **We must not disturb any of this.**

---

## 2. Global constraints (apply to every task)

1. **Additive only.** Do not modify the working connect handshake, the recording write path,
   the proto/WS message contract, the reconnect/buffer logic, or device lifecycle. Every
   change is a new file, a new function, a new UI affordance, a new manifest permission, or
   an extra field on an existing JSON response.
2. **Branch:** all work on `experiment/operational-frontend-fixes`. Never touch `master`/`main`.
3. **No new backend dependencies.** Frontend: no new npm packages. Mobile: **no new pub
   packages** — `flutter_foreground_task` (already present) provides everything for #3.
4. **Windows-first.** Primary shell is PowerShell. Any launcher must run with zero extra
   tooling (no requirement to install `make`).
5. **Don't regress `/health` or the WS JSON shape.** Only *add* fields; never rename/remove.

---

## 3. ISSUE 1 — "Can't connect" feedback + easy backend-IP config check

### 3.1 Root cause

- **Frontend, dead-end error.** On manual connect, failure produces exactly
  `` `Cannot reach ${backendIp}:8000` `` (`page.tsx:142`). On auto-reconnect it fails
  *silently* after 5 s (`page.tsx:117-120`). The operator gets no clue whether the backend
  is down, the IP is wrong, the firewall is blocking, or the phone/laptop are on different
  subnets.
- **No way to read the backend's IP without stopping it.** `/health` (`main.py:67-75`)
  returns status/version/session but **not the LAN IP or the WS URLs**. The startup log says
  `listening on 0.0.0.0:8000` (`main.py:43-44`) — `0.0.0.0` is not a dialable address. So to
  learn "what IP do I type into the phone?", the operator today stops the server and inspects
  `.env`/`ipconfig`. That is the friction the peer described.
- **Mobile, generic error.** `connection_screen.dart:62` shows
  `` 'Could not connect to $ip:8000' `` with no reason. `WebSocketClient.connect()` swallows
  the actual exception in its `catch (e)` (`websocket_client.dart:122-129`) and returns a
  bare `false` — the caught reason (timeout vs refused vs bad host) is discarded.

### 3.2 Fix design (three cooperating pieces, all additive)

**A one-line answer to "check backend IP without stopping":** a standalone `ops/ip.ps1`
("doctor") the operator runs in a *second* terminal, plus the same info surfaced in `/health`
and a startup banner, plus an in-dashboard diagnostic when connect fails.

---

#### Task 1.1 — Backend: print a clear startup banner with the real LAN IP

**File:** `master_backend/app/main.py`
**Change:** additive log lines in `lifespan()` startup, reusing the existing `_get_local_ip()`.

After `mDNS` registration (near `main.py:43`), replace the single terse `logger.info(...)`
line with a multi-line banner (keep it a log, not a print, so `--reload` is fine):

```python
    local_ip = _get_local_ip()
    port = int(os.getenv("PORT", "8000"))
    logger.info(
        "\n"
        "==================================================================\n"
        "  IMU Telemetry Backend — READY\n"
        "  Dashboard (this laptop) : http://localhost:3000\n"
        "  Backend LAN IP          : %s:%s   <-- type THIS into phones & dashboard\n"
        "  Phone telemetry WS      : ws://%s:%s/ws/telemetry\n"
        "  Phone control   WS      : ws://%s:%s/ws/control\n"
        "  Health check            : http://%s:%s/health\n"
        "==================================================================",
        local_ip, port, local_ip, port, local_ip, port, local_ip, port,
    )
```

- **Why additive-safe:** it only changes a log message. No behavior change.
- **Acceptance:** starting the backend prints the dialable IP prominently; the operator
  never has to open `.env` to find it.

---

#### Task 1.2 — Backend: add IP + WS URLs to `/health`

**File:** `master_backend/app/main.py`, `health()` (`main.py:67-75`).
**Change:** add fields; do not remove existing ones.

```python
@app.get("/health")
async def health():
    local_ip = _get_local_ip()
    port = int(os.getenv("PORT", "8000"))
    return {
        "status": "ok",
        "version": "2.0.0",
        "session_state": session_manager.state,
        "session_id": session_manager.session_id or None,
        "online_devices": len(session_manager.online_devices),
        # --- added (additive) ---
        "lan_ip": local_ip,
        "port": port,
        "ws_telemetry": f"ws://{local_ip}:{port}/ws/telemetry",
        "ws_control": f"ws://{local_ip}:{port}/ws/control",
    }
```

- **Why:** CORS is already `*` (`main.py:57-62`), so the browser dashboard and the `ops/ip.ps1`
  doctor can both read this cross-origin while the backend keeps running.
- **Acceptance:** `curl http://<ip>:8000/health` (or the dashboard diagnostic) shows the LAN IP
  and WS URLs live, no restart needed.

---

#### Task 1.3 — New CLI "doctor": `ops/ip.ps1` (+ `make ip`)

**New file:** `ops/ip.ps1` — runnable anytime in a second terminal *without* touching the
running backend. This is the direct answer to "cek config IP backend, sekarang harus stop
program dulu".

It must:
1. Print **every** IPv4 the laptop owns (so the operator can pick the Wi-Fi one), marking the
   likely LAN one (`192.168.*` / `10.*` / `172.16-31.*`).
2. Print the exact strings to type into the phone and dashboard: `IP:8000` and the WS URLs.
3. Check whether **port 8000 is listening** (`Get-NetTCPConnection -LocalPort 8000`), i.e. "is
   the backend even up?".
4. Probe `http://<ip>:8000/health` and print `status/session_state/online_devices`.
5. Print a one-line firewall hint if port 8000 is not reachable.

```powershell
# ops/ip.ps1 — show backend connection config WITHOUT stopping the server.
$port = 8000
Write-Host "=== IMU Backend Connection Doctor ===" -ForegroundColor Cyan

$ips = Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '127.0.0.1' }
foreach ($ip in $ips) {
    $lan = $ip.IPAddress -match '^(192\.168|10\.|172\.(1[6-9]|2[0-9]|3[01]))\.'
    $tag = if ($lan) { '  <-- use this on the phones' } else { '' }
    Write-Host ("  {0}  ({1}){2}" -f $ip.IPAddress, $ip.InterfaceAlias, $tag)
}

$listening = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($listening) { Write-Host "Port $port : LISTENING (backend is up)" -ForegroundColor Green }
else { Write-Host "Port $port : NOT listening — start the backend first" -ForegroundColor Red }

$lanIp = ($ips | Where-Object { $_.IPAddress -match '^(192\.168|10\.|172\.(1[6-9]|2[0-9]|3[01]))\.' } |
    Select-Object -First 1).IPAddress
if ($lanIp) {
    Write-Host ""
    Write-Host "Type into phone / dashboard:  $lanIp" -ForegroundColor Yellow
    Write-Host "  ws://$lanIp`:$port/ws/control"
    Write-Host "  ws://$lanIp`:$port/ws/telemetry"
    try {
        $h = Invoke-RestMethod "http://$lanIp`:$port/health" -TimeoutSec 2
        Write-Host ("Health OK — state=$($h.session_state) devices=$($h.online_devices)") -ForegroundColor Green
    } catch {
        Write-Host "Health probe failed — check Windows Firewall inbound rule for port $port on the Wi-Fi (Private) network." -ForegroundColor Red
    }
}
```

- **Acceptance:** operator opens a new terminal, runs `.\ops\ip.ps1` (or `make ip`), and sees
  the IP to type + whether the backend is reachable — all while a session keeps running.

---

#### Task 1.4 — Frontend: turn the connect failure into an actionable diagnostic

**File:** `master_frontend/src/app/page.tsx`.
**Changes (additive):**

1. On connect failure, instead of only `` `Cannot reach ${backendIp}:8000` ``, run a quick
   HTTP probe to distinguish *backend-down* from *WS-blocked*, and render a short checklist.

Add a helper (module scope, near `_downloadBlob`):

```tsx
async function probeBackend(ip: string): Promise<
  { ok: true; lanIp?: string } | { ok: false; reason: string }
> {
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 2000);
    const res = await fetch(`http://${ip}:8000/health`, { signal: ctrl.signal });
    clearTimeout(t);
    if (!res.ok) return { ok: false, reason: `Backend answered HTTP ${res.status}` };
    const j = await res.json();
    return { ok: true, lanIp: j.lan_ip };
  } catch {
    return { ok: false, reason: "No HTTP response (backend not started, wrong IP, or firewall/subnet)" };
  }
}
```

2. In `handleConnect` (`page.tsx:127-145`), on the timeout branch (`tries > 25`), call
   `probeBackend(backendIp)` and set a richer, multi-line `connectError`. If
   `probe.ok === true` but the WS still failed, the message points at the WS specifically
   ("HTTP reachable but WebSocket did not open — check that this is the backend, not another
   service on :8000"). If `probe.ok === false`, show the reason + a 4-item checklist:
   - Backend running? (run `ops\ip.ps1` on the laptop)
   - Same Wi-Fi / subnet as the laptop?
   - IP correct? (the doctor prints it)
   - Firewall allows inbound TCP 8000 on the Private network?
   Also, if the probe returns `lanIp` and it differs from `backendIp`, surface: "Backend
   reports its IP is **{lanIp}** — try that." (This is the biggest real-world win: a DHCP
   lease change silently moved the laptop's IP.)

3. **Make the config reachable without leaving a running session.** Today the only route back
   to the IP field is the "Disconnect" button (correctly locked during RECORDING). Add a tiny,
   always-visible read-only line in the connect screen and in `StatusBanner` showing the
   currently-targeted IP, e.g. `Backend: 192.168.1.100 · connected`. Purely informational; no
   behavior change. (Optional stretch: a non-destructive "Change IP" that only re-points the
   WS when **not** RECORDING; keep it out of scope if it risks the recording path.)

- **Why additive-safe:** the happy path (`wsClient.isConnected` becomes true) is untouched;
  the new code only runs on the failure/timeout branch and adds display-only text.
- **Acceptance:** a failed connect explains *why* and *what to try*, and (when possible) tells
  the operator the backend's real IP.

---

#### Task 1.5 — Mobile: surface the real connect-failure reason

**Files:** `mobile_node/lib/services/websocket_client.dart`,
`mobile_node/lib/screens/connection_screen.dart`.
**Change (additive, no control-flow change):**

1. In `WebSocketClient`, capture the caught exception into a new public getter without
   altering the return contract:

```dart
String? _lastConnectError;
String? get lastConnectError => _lastConnectError;
```

In `connect()`’s `catch (e)` (`websocket_client.dart:122-129`), set
`_lastConnectError = _describeConnectError(e);` before `_setState(WsState.offline)`. Add a
small classifier:

```dart
String _describeConnectError(Object e) {
  final s = e.toString().toLowerCase();
  if (s.contains('timeout')) return 'Timed out — laptop unreachable. Same Wi-Fi? Backend running? IP correct?';
  if (s.contains('refused')) return 'Connection refused — backend not started on :8000 at this IP.';
  if (s.contains('failed host lookup') || s.contains('no address')) return 'Bad IP address — re-check the number.';
  if (s.contains('network is unreachable')) return 'Phone not on the same network as the laptop.';
  return 'Could not connect. Check Wi-Fi, backend status, and the IP.';
}
```

Clear `_lastConnectError = null;` at the top of `connect()` on a fresh attempt.

2. In `connection_screen.dart._connect()` (`:39-65`), when `ok == false`, prefer the detailed
   reason:

```dart
_error = WebSocketClient().lastConnectError ?? 'Could not connect to $ip:8000';
```

- **Why additive-safe:** `connect()` still returns `bool`; the new getter is read-only; the
  screen still falls back to the old string if the reason is null. No handshake timing changes.
- **Acceptance:** the phone shows *why* it couldn't connect, not just *that* it couldn't.

---

## 4. ISSUE 2 — All-in-one launcher ("assume env exists + frontend built, just start")

### 4.1 Root cause / reality check

- The existing `Makefile` only has **separate** `run-backend` and `run-frontend` targets
  (`Makefile:38-44`), each of which blocks. There is no single "start everything" entry point.
- **`make` is not installed** on the Windows host. Shipping only a Makefile means the peer
  can't run it. So the *real* deliverable is a **Windows-native launcher**, with a Makefile
  target that simply calls it (works for anyone who does have `make`/`mingw32-make`, and
  documents the intent).
- Two blocking servers can't run in one sequential `make` recipe on Windows; they must launch
  in **separate windows**. PowerShell `Start-Process` is the clean way.

### 4.2 Design

Deliverables (all new files, nothing existing is altered except *adding* Makefile targets):

```
start.ps1        # primary all-in-one launcher (backend + frontend, new windows, prints IP banner)
start.bat        # double-click wrapper that calls start.ps1 (for non-terminal users)
ops/ip.ps1       # the doctor from Task 1.3 (shared)
ops/stop.ps1     # optional: stop backend+frontend (kill :8000 and the next server)
Makefile         # ADD targets: start, ip, doctor, stop  (existing targets untouched)
```

#### Task 2.1 — `start.ps1` (the all-in-one)

Behavior, in order:

1. **Banner + IP** — call the same logic as `ops/ip.ps1` (or dot-source it) so the very first
   thing the operator sees is "type THIS IP into the phones".
2. **Preflight (warn, don't hard-fail)** — the peer's assumption is "env sudah ada, frontend
   sudah dibuild", so *check* but continue with a clear warning if something's missing:
   - `master_backend/venv/Scripts/python.exe` exists? (else: "run `make install-backend` / create venv")
   - `master_backend/.env` exists? (it does today)
   - `master_frontend/node_modules` exists? (else: "run `npm install`")
   - `master_frontend/.next/BUILD_ID` exists? → decides prod vs dev (see step 4).
   - Port 8000 free? If already LISTENING, warn "backend already running — skipping backend".
3. **Start backend** in a new window using the venv Python, from repo root:
   ```powershell
   # PORTABILITY: derive repo root from the script's own location — NEVER hardcode a path
   # or an IP, so any peer can `git pull` and run this unchanged on their laptop.
   $root = $PSScriptRoot
   $venvPy = Join-Path $root 'master_backend\venv\Scripts\python.exe'
   $py = if (Test-Path $venvPy) { $venvPy } else { 'python' }
   Start-Process -FilePath $py -ArgumentList 'master_backend/run.py' -WorkingDirectory $root
   ```
   (Uses the existing `run.py` → uvicorn on `0.0.0.0:8000`. No change to backend runtime.)
4. **Start frontend** in a new window:
   ```powershell
   $fe = Join-Path $root 'master_frontend'
   $hasBuild = Test-Path (Join-Path $fe '.next\BUILD_ID')
   $script = if ($hasBuild) { 'start' } else { 'dev' }   # prod if built, else dev
   Start-Process -FilePath 'npm.cmd' -ArgumentList 'run', $script -WorkingDirectory $fe
   ```
   - If the peer wants **strictly** "already built, just start" (prod), and `BUILD_ID` is
     missing, print: "No production build found — starting dev server instead. Run
     `npm run build` in master_frontend for prod." (Non-fatal; keeps them moving.)
5. **Open the dashboard** (optional convenience): `Start-Process 'http://localhost:3000'`.
6. Print: "Backend + Frontend launched in separate windows. Close those windows (or run
   `ops\stop.ps1`) to stop."

> **Design note:** deliberately launch in **separate windows** (not background jobs) so the
> operator can see each server's logs — including the backend IP banner from Task 1.1 — and
> Ctrl-C either independently. This matches how the system is run today; we're only removing
> the "two manual commands" friction.

#### Task 2.2 — `start.bat` (double-click)

```bat
@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
```

- `%~dp0` keeps it path-independent. `-ExecutionPolicy Bypass` avoids the unsigned-script
  block for a local dev tool.

#### Task 2.3 — `ops/stop.ps1` (optional but recommended)

Kill whatever is listening on 8000 (backend) and 3000 (next), by PID:

```powershell
foreach ($p in 8000, 3000) {
  $conns = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
  foreach ($c in $conns) { Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue }
}
Write-Host "Stopped backend (:8000) and frontend (:3000) if they were running."
```

#### Task 2.4 — Makefile targets (existing targets untouched)

Append to `Makefile` (these call the PowerShell scripts so they work on this host if `make`
is ever installed, and they *document* the entry points):

```makefile
## Start backend + frontend together (assumes env + build ready)
start:
	powershell -NoProfile -ExecutionPolicy Bypass -File start.ps1

## Show backend LAN IP + health without stopping the server
ip doctor:
	powershell -NoProfile -ExecutionPolicy Bypass -File ops/ip.ps1

## Stop backend + frontend
stop:
	powershell -NoProfile -ExecutionPolicy Bypass -File ops/stop.ps1
```

And extend `.PHONY` (`Makefile:7`) with `start ip doctor stop`.

- **Reality note for the peer:** since `make` isn't installed, the primary invocation is
  `.\start.ps1` (or double-click `start.bat`) and `.\ops\ip.ps1`. Document both. If they want
  `make start` to actually work, they install `make` (choco/scoop) or alias
  `mingw32-make` → `make`; the target is ready for that day.

- **Acceptance:** from a fresh terminal at repo root, `.\start.ps1` prints the backend IP,
  launches both servers in their own windows, and opens the dashboard — one action, no manual
  IP hunting.

---

#### Task 2.5 — Update `STARTUP.md` (do this LAST, once script + doc filenames are final)

`STARTUP.md` is the file peers open first, so it must point at the new tooling. Keep the
existing manual instructions (some peers still want them) but add the fast path on top and
cross-link the new docs. Concrete edits:

1. **Add a "Quick start (all-in-one)" section at the very top**, above the current manual
   Backend/Frontend sections:
   ```markdown
   ## Quick start (all-in-one)

   Assumes env + build already done (see first-time setup below if not):

   - Double-click **`start.bat`**, or run **`.\start.ps1`**, or **`make start`**.
   - It prints the **Backend LAN IP** to type into the phones, then launches backend +
     frontend in separate windows and opens the dashboard.

   To check the backend IP anytime WITHOUT stopping it, run **`.\ops\ip.ps1`** (or `make ip`)
   in a second terminal.
   ```

2. **Replace the "Finding your laptop IP" section** (`STARTUP.md:108-114`, the
   `ipconfig | findstr "IPv4"` snippet) — keep `ipconfig` as a fallback but lead with:
   ```markdown
   Run `.\ops\ip.ps1` — it lists every IPv4, marks the LAN one to use, checks whether the
   backend is up on :8000, and probes /health. (Fallback: `ipconfig | findstr "IPv4"`.)
   ```

3. **Update the "Common issues" table** (`STARTUP.md:118-126`):
   - "Frontend can't reach backend" → *"Dashboard now shows the reason + a checklist on a
     failed connect; run `.\ops\ip.ps1` to confirm the backend IP and that :8000 is
     listening. Check the Windows Firewall inbound rule for TCP 8000 on the Private network."*
   - "Flutter can't find server via mDNS" → *"Enter the laptop IP manually (the phone now
     shows a specific failure reason); if it drops mid-session on Xiaomi/Redmi, see
     `docs/REDMI_NOTE_12_SETUP.md`."*
   - Add a row: *"Phone disconnects mid-session (Redmi/Xiaomi) | Apply
     `docs/REDMI_NOTE_12_SETUP.md` per phone (Autostart, Battery = No restrictions, lock in
     recents, keep Wi-Fi on during sleep). Data is buffered + re-flushed, not lost."*

4. **Add a "For peers pulling this repo" note** near the top, linking to §9a of
   `connectivity_ops_fixes_plan.md`: peers must be on the `experiment/operational-frontend-fixes`
   branch and have their own `.env` (from `.env.example`), `venv`, and `node_modules`; the
   phones need a freshly built APK.

5. **Cross-link the new docs** at the bottom: `docs/REDMI_NOTE_12_SETUP.md` (per-phone setup)
   and `connectivity_ops_fixes_plan.md` (what changed and why).

- **Why last:** the exact script names, `make` target names, and the SOP filename must be
  final before they're referenced, or the doc rots. Purely documentation — zero runtime risk.
- **Acceptance:** a peer who opens `STARTUP.md` cold can start everything, find the backend IP
  without stopping the server, and knows where to look when a phone drops.

---

## 5. ISSUE 3 — Mid-session phone disconnect on Redmi Note 12 (app vs phone?)

### 5.1 The verdict, with evidence

**Primarily a phone (MIUI/HyperOS) power-management behavior — not an app logic bug.**
Redmi Note 12 ships MIUI 14 / HyperOS on Android 13, which is aggressive about:

- **Freezing/killing apps when the screen is off**, *even foreground services*, unless the
  app is Autostart-enabled, exempt from battery optimization, and "locked" in recents.
- **Dropping/throttling Wi-Fi in sleep** (Doze) to save power.
- **Suppressing notifications** for apps that lack the Android-13 `POST_NOTIFICATIONS`
  runtime grant — and a foreground service whose notification is suppressed is *more* likely
  to be reaped.

**Evidence the app is not the cause of data loss and already copes with drops:**

- Drops are detected on both sockets (`onDone`/`onError`, `websocket_client.dart:89-110`).
- Offline → packets buffer to disk (`_onSensorPacket` else-branch, `:175-186`).
- Reconnect retries every 3 s (`_scheduleReconnect`, `:278-286`) and re-flushes the buffer,
  which the backend dedups (`ws_handler.py:79-82`) → no duplicate rows, no lost samples.
- HANDOFF.md already flags this exact OEM behavior as "not fixable in code" — correct, but
  **incomplete**: the app can still make itself a much harder target.

**Where the app is genuinely at fault (hygiene, ~20%):** it **never** asks Android for the
two things that most reduce MIUI kills:

1. **Battery-optimization exemption.** `flutter_foreground_task 8.17.0` (already a dependency)
   exposes `isIgnoringBatteryOptimizations`, `requestIgnoreBatteryOptimization()`,
   `openIgnoreBatteryOptimizationSettings()` — **none are called anywhere**, and the app
   manifest lacks `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`.
2. **Android-13 notification permission.** The app manifest does not request
   `POST_NOTIFICATIONS`; the app never calls `requestNotificationPermission()`. On the
   Redmi (Android 13) the FGS notification may be hidden, weakening the service.

WifiLock/WakeLock *are* already requested via `foregroundTaskOptions`
(`foreground_service_handler.dart:27-33`) — good; keep them.

### 5.2 A decisive test to confirm app-vs-phone (do this first, ~10 min)

Run this before/after the app fixes so the peer *sees* the cause:

| Step | What to watch | Interpretation |
|------|---------------|----------------|
| Start a session, lay the Redmi down, **screen OFF**, don't move it 5–10 min | Does the "IMU Telemetry" **notification stay** in the shade? | Notification vanishes → **MIUI killed the app** (phone). Stays → service alive. |
| While screen off, watch the **dashboard** | Device flips OFFLINE after ~8 s? | Yes → the socket dropped (phone Doze/Wi-Fi), not an app crash. |
| Wake the phone | Does it **auto-reconnect** and does the notification show "**buffered N**" then drain? | Yes → app coped; the drop was power/network. **Data is safe.** |
| Check `SSD_PATH/backend_audit.jsonl` | `ws_disconnect` (control/telemetry) timestamps line up with screen-off | Confirms drops correlate with screen-off, i.e. power management. |
| Repeat with **screen ON** (or charger + Task 5.3 applied) | Do the drops **stop**? | Stopping → confirms **phone power management** is the driver. |

**Expected result:** drops correlate with screen-off and stop when the phone is exempted +
notification-granted + Autostart-on. That is the proof it's the phone, mitigated by app+SOP.

### 5.3 App-side fixes (additive, no new dependency)

#### Task 3.1 — Manifest: declare the battery-optimization request permission

**File:** `mobile_node/android/app/src/main/AndroidManifest.xml` (near the FGS permissions,
`:16-20`). Add:

```xml
    <!-- Allow the app to prompt the user to exempt it from battery optimization (MIUI kills otherwise) -->
    <uses-permission android:name="android.permission.REQUEST_IGNORE_BATTERY_OPTIMIZATIONS"/>
```

(`POST_NOTIFICATIONS` is already merged in from the plugin manifest, so no need to re-declare;
we only need to *request* it at runtime — Task 3.2.)

#### Task 3.2 — Request notification + battery-opt exemption once, up front

**File:** `mobile_node/lib/services/foreground_service_handler.dart` — add a method
(idempotent, guarded so re-calls are cheap):

```dart
Future<void> ensurePermissions() async {
  // Android 13+ needs the notification grant for the FGS notification to show,
  // which in turn makes MIUI far less likely to reap the service.
  final np = await FlutterForegroundTask.checkNotificationPermission();
  if (np != NotificationPermission.granted) {
    await FlutterForegroundTask.requestNotificationPermission();
  }
  // The single biggest lever against MIUI/HyperOS killing us in the background.
  if (!await FlutterForegroundTask.isIgnoringBatteryOptimizations) {
    await FlutterForegroundTask.requestIgnoreBatteryOptimization();
  }
}
```

**Call site:** `mobile_node/lib/screens/connection_screen.dart`, in `initState()` (after
`_load()`), fire-and-forget so it never blocks the UI:

```dart
ForegroundServiceHandler().ensurePermissions();
```

- **Why here:** the connection screen is the first screen, before any recording, and the
  request is idempotent (guarded by `isIgnoringBatteryOptimizations`). It shows the system
  "Allow to run in background?" dialog once, then never again.
- **Why additive-safe:** does not touch `connect()`, sensors, or the FGS lifecycle; it only
  asks for permissions. If the user denies, everything still works exactly as today (the SOP
  in Task 3.4 becomes the fallback).

#### Task 3.3 — Add an in-app "Fix background killing" affordance (optional, recommended)

**File:** `connection_screen.dart` — a small text button under CONNECT:
`"Phone keeps disconnecting? Tap to fix background limits"` →
`FlutterForegroundTask.openIgnoreBatteryOptimizationSettings()`. Because MIUI **Autostart**
has no public API, also show one line: "On Xiaomi/Redmi also enable Autostart + set Battery to
No restrictions (see setup card)." This gives the operator a one-tap path in the field.

- **Not** keep-screen-on: we deliberately avoid adding a `wakelock`/keep-screen dependency.
  WifiLock + WakeLock (already on) + battery-opt exemption + Autostart is the correct,
  dependency-free stack. If, after the decisive test, drops *still* happen with screen off,
  the fallback is "record with screen on / phone on charger", documented in the SOP — not a
  code change.

### 5.4 The guaranteed fix — per-device OS setup (the real lever)

#### Task 3.4 — New doc: `docs/REDMI_NOTE_12_SETUP.md` (operator SOP, one card per phone)

Because the definitive fix is OS settings with no API, ship a crisp per-phone checklist. It
must cover (MIUI 14 / HyperOS paths):

1. **Autostart ON** — Settings → Apps → Manage apps → *IMU Node* → **Autostart** = on.
2. **Battery = No restrictions** — same app page → **Battery saver** → **No restrictions**.
3. **Battery-optimization exemption** — Settings → Battery → App battery saver → *IMU Node* →
   **Don't optimize** (also triggered by Task 3.2's in-app prompt).
4. **Lock in recents** — open recents, swipe down on the *IMU Node* card → **padlock** so
   MIUI's memory cleaner can't swipe it away.
5. **Keep Wi-Fi on during sleep** — Settings → Wi-Fi → Additional settings →
   **Keep Wi-Fi on during sleep = Always** (kills the Doze Wi-Fi drop).
6. **Notifications ON** for *IMU Node* (so the FGS notification shows; Task 3.2 requests it).
7. **Disable MIUI "Memory extension"/aggressive cleanup** if enabled.
8. **Field fallback:** if a specific unit still drops, record with the **screen on** (dim
   brightness) or on a **power bank** — physically prevents Doze.

Include a 30-second **verification**: apply settings → start a session → screen off 5 min →
confirm the dashboard stays ONLINE and CSV rows are continuous.

- **Acceptance:** with Task 3.1–3.2 (app) + Task 3.4 (per phone) applied, the decisive test in
  §5.2 shows no mid-session offline flips with the screen off.

---

## 6. File-by-file change inventory

| File | New/Edit | Issue | Change | Risk |
|------|----------|-------|--------|------|
| `master_backend/app/main.py` | Edit | 1 | Startup IP banner (log only); add `lan_ip/port/ws_*` to `/health` | Very low (log + additive JSON) |
| `ops/ip.ps1` | **New** | 1,2 | Connection doctor (IP, port, health) | None (read-only) |
| `master_frontend/src/app/page.tsx` | Edit | 1 | `probeBackend()` + richer `connectError` on failure branch; show target IP (display-only) | Low (failure branch only) |
| `mobile_node/lib/services/websocket_client.dart` | Edit | 1 | `lastConnectError` getter + `_describeConnectError()`; set in existing `catch` | Low (return type unchanged) |
| `mobile_node/lib/screens/connection_screen.dart` | Edit | 1,3 | Use `lastConnectError`; call `ensurePermissions()` in `initState`; optional "fix background" button | Low |
| `start.ps1` | **New** | 2 | All-in-one launcher (banner + preflight + start both) | None (orchestration) |
| `start.bat` | **New** | 2 | Double-click wrapper | None |
| `ops/stop.ps1` | **New** | 2 | Stop :8000/:3000 | Low (kills by port) |
| `Makefile` | Edit | 2 | Add `start`/`ip`/`doctor`/`stop` targets + `.PHONY` | None (existing targets intact) |
| `mobile_node/android/app/src/main/AndroidManifest.xml` | Edit | 3 | Add `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS` | Low (permission only) |
| `mobile_node/lib/services/foreground_service_handler.dart` | Edit | 3 | Add `ensurePermissions()` (notification + battery-opt) | Low (additive method) |
| `docs/REDMI_NOTE_12_SETUP.md` | **New** | 3 | Per-device MIUI SOP | None (doc) |
| `STARTUP.md` | Edit | 1,2,3 | Link the launcher, the doctor, and the Redmi SOP | None (doc) |

**Explicitly NOT touched:** the connect handshake in `WebSocketClient.connect()` control flow,
sensor sampling, the FGS start/stop lifecycle, `ws_handler.py`, `session_manager.py`, the
proto/WS contract, the recording write path, `run.py`, and `master`/`main`.

---

## 7. Rollout / implementation order

1. **Issue 2 first** (pure new files) — `ops/ip.ps1`, `start.ps1`, `start.bat`, `ops/stop.ps1`,
   Makefile targets. Zero risk, immediately useful, and `ops/ip.ps1` is reused by Issue 1.
2. **Issue 1 backend** — banner + `/health` fields (verify `curl /health` shows `lan_ip`).
3. **Issue 1 frontend** — `probeBackend` + richer error (verify by connecting to a wrong IP).
4. **Issue 1 mobile** — `lastConnectError` (verify by pointing the phone at a wrong IP).
5. **Issue 3 app** — manifest permission + `ensurePermissions()` (verify the two system
   dialogs appear on first launch of a rebuilt APK).
6. **Issue 3 SOP** — write `docs/REDMI_NOTE_12_SETUP.md`; run the §5.2 decisive test on a real
   Redmi Note 12 to confirm drops stop with screen off.
7. **Docs last (Task 2.5)** — update `STARTUP.md` once all script names, `make` targets, and
   `docs/REDMI_NOTE_12_SETUP.md` are finalized, so nothing it references is renamed afterward.

**Build/deploy reminders:**
- Backend: restart to pick up `main.py` (or `--reload` catches it).
- Frontend: `npm run dev` hot-reloads; a prod change needs `npm run build`.
- Mobile: Issues 1 & 3 change Dart + manifest → **`flutter build apk --release` + reinstall**
  on each phone. The permission dialogs only appear on the rebuilt APK.

---

## 8. Verification checklist (Definition of Done)

- [ ] `.\start.ps1` launches backend + frontend in separate windows and prints the LAN IP.
- [ ] `.\ops\ip.ps1` (and `make ip`) prints the dialable IP + WS URLs + health, **while a
      session is running** (no stop required).
- [ ] Backend console banner shows `Backend LAN IP : <ip>:8000` on startup.
- [ ] `curl http://<ip>:8000/health` returns `lan_ip` and `ws_control`/`ws_telemetry`.
- [ ] Dashboard: connecting to a wrong IP shows a *reason* + checklist (and the backend's real
      IP if HTTP-reachable), not just "Cannot reach".
- [ ] Phone: connecting to a wrong IP shows a specific reason (timeout/refused/bad-IP).
- [ ] Rebuilt APK on first launch prompts for notifications + battery-optimization exemption.
- [ ] After applying `docs/REDMI_NOTE_12_SETUP.md` on a Redmi Note 12, the §5.2 test shows the
      device stays ONLINE with the screen off, and CSV rows are continuous (no data gap).
- [ ] No regression: existing connect, recording, reconnect, buffering, and stop flows behave
      exactly as before.

---

## 9a. How peers actually get this (git + prerequisites) — READ BEFORE HANDOFF

This plan is a **design doc**. For a peer to `git pull` and run the launcher, the flow is:

1. **Implement** the tasks above, then **commit + push** to `experiment/operational-frontend-fixes`
   (or merge to whatever branch peers track). Verified none of the new files are gitignored:
   `start.ps1`, `start.bat`, `ops/*.ps1`, and this plan **will** travel with the repo.
2. **Peer checks out the same branch** (`git fetch && git checkout experiment/operational-frontend-fixes && git pull`).
   They are probably on a different branch — the scripts don't exist on `master`.
3. **Peer must already have (NOT distributed by git):**
   - `master_backend/.env` — **gitignored** (`.gitignore:283`). Create from the tracked
     `.env.example`. The launcher warns if it's missing.
   - `master_backend/venv/` — their own virtualenv (machine-specific; do **not** commit it —
     note: `.gitignore` ignores `.venv/` but not `venv/`, so be careful not to stage it).
   - `master_frontend/node_modules/` — `npm install` (ignored, `.gitignore:286`).
   - `master_frontend/.next/` build — only needed for prod `next start`; the launcher falls
     back to `dev` if absent, so "just start" still works.
4. **Portability guarantees** (so it runs on their laptop, not just this one):
   - `start.ps1` derives repo root from `$PSScriptRoot`; `start.bat` uses `%~dp0`. **No
     hardcoded paths.**
   - The IP is **computed at runtime** (`Get-NetIPAddress`) — never hardcoded. Each laptop
     shows its own LAN IP.
   - Run via **`start.bat`** or **`make start`** (both pass `-ExecutionPolicy Bypass`). If a
     peer runs `.\start.ps1` directly and hits the unsigned-script block, that's the fix:
     use the `.bat`, or `powershell -ExecutionPolicy Bypass -File start.ps1`.
5. **The phones do NOT update from git.** Issues 1 & 3 are APK changes — rebuild
   (`flutter build apk --release`) and reinstall on each phone. Pull only updates the laptop.

**One-line answer to "peers can pull and run the makefile?":** yes **after** it's implemented
and pushed, they check out this branch, and they have their own `.env` + `venv` + `node_modules`
in place — the scripts themselves are path/IP-independent and need no per-machine editing. The
**phones still need a fresh APK** separately.

---

## 9. Answering the peer directly (paste-ready)

- **"Kasih detail kalau gagal connect + cara cek IP tanpa stop program":** done via (a) a
  backend startup banner + `/health` that expose the real LAN IP, (b) `ops\ip.ps1` / `make ip`
  you run in a second terminal anytime to see the IP + whether the backend is up, and (c) the
  dashboard/phone now explain *why* a connect failed and suggest the fix.
- **"Makefile all-in-one":** `make` isn't installed on the laptop, so the real one-shot is
  **`.\start.ps1`** (or double-click **`start.bat`**) — it assumes env+build exist, prints the
  IP, and starts backend+frontend together. A `make start` target is also added for when `make`
  is installed.
- **"HP disconnect di tengah — app atau phone?":** **mostly the phone.** The Redmi Note 12
  (MIUI/HyperOS) freezes the app/Wi-Fi when the screen is off. The app already survives it
  (buffers + reconnects, no data lost), but it wasn't asking Android for the battery-optimization
  exemption or the notification permission — we add both. The *guaranteed* fix is the per-phone
  setup card (`docs/REDMI_NOTE_12_SETUP.md`): Autostart on, Battery = No restrictions, lock in
  recents, keep Wi-Fi on during sleep. Do the 10-minute test in §5.2 to see it for yourself.
```
