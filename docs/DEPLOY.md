# Deploy checklist

Run this before every recording day, and after every `git pull`.

It exists because of a specific failure. On **2026-08-11** an operator lost the export of a
17-minute session with a live subject. The backend was flawless — it finalised, retiered the
CSVs, validated and returned to IDLE with zero errors, and all 103k rows per device were on
disk. What failed was the dashboard: the browser was running a **cached JavaScript bundle
from an older deploy** against a newer backend, and it crashed at the moment of saving.

The rebuild removes most of that failure surface: the dashboard is now a static build
**committed inside the backend** (`master_backend/app/static/`), served from the same origin
on the same port, so backend and dashboard are always the same commit and there is no second
server to restart. What is left is *cache* — and one build id, which is why the checks below
are short but not optional.

---

## 1. One commit on the laptop

```bash
git pull
git rev-parse --short HEAD      # note this — you will confirm it in step 2
```

If the pull touched `dashboard/src/`, the committed bundle is stale: rebuild it on a machine
with Node and commit the result **together with the source change**.

```bash
make dashboard                  # cd dashboard && npm ci && npm run build
git add master_backend/app/static && git commit
```

Then restart the backend (`.\start_all.ps1` or `.\ops\stop.ps1` + `start.bat`). A running
process keeps serving the old files until it is restarted.

## 2. The dashboard build id matches `/health`

Open the dashboard. The **top bar** shows `build <sha>`; hover it and it names the backend's
build. Compare with:

```
GET http://<laptop>:8000/health   ->   "build_id": "<sha>"
```

They must be identical. If they differ, the laptop's commit is not the one that built the
bundle: rebuild (step 1) or `git pull` again. If the browser looks wrong but the ids match,
**Ctrl+Shift+R** — nothing is stored in the browser, so a reload cannot lose anything.

`build_id` reads `unknown` when the checkout is not a git repository (e.g. a zipped copy).
That is not an error, but it means you cannot use this check — verify the git sha you built
from instead.

## 3. Every phone on the same app version

```bash
make apk                              # cd mobile_node && ./gradlew assembleRelease
./ops/install_phones.sh               # make install-phones — never uninstalls
```

The script prints the version it installed and fails loudly on a signature mismatch (it
stops *before* anything is uninstalled: the phone-local rescue CSVs do not survive an
uninstall).

Then confirm on the dashboard: **Setup → Phones**, each phone card shows `app <version>`.
All handsets in one study must match, and a mismatched phone raises a readiness *warning*
(the session records it as `preflight_failed` if you start anyway).

## 4. ffmpeg present

The cameras are captured on the backend now; without ffmpeg there is no preview and no video
in the session ZIP.

```powershell
.\ops\ip.ps1                          # prints the ffmpeg binary it found, and /health
```

It must not print `ffmpeg : NOT FOUND`. Accepted locations: `IMU_FFMPEG`/`IMU_FFPROBE` in
`master_backend\.env`, `vendor\ffmpeg\bin\`, then `PATH`. Details and the download hint:
[`../STARTUP.md`](../STARTUP.md) §1.2.

Also check that the cameras are actually visible: Setup → Cameras lists the dshow devices.
A webcam opened by Teams/Zoom/Camera does not appear — close those programs, `.\ops\stop.ps1`,
start again.

## 5. Before attaching sensors to a person

The Setup screen computes readiness on the backend and splits it:

**Blockers** (START is disabled, each says how to fix it): the backend can write to
`SSD_PATH` with ≥ 5 GB free; ≥ 1 phone online; Subject/Tag/Operator filled; ≥ 1 camera
selected and every selected camera live; no session already recording or finalising.

**Warnings** (START allowed, the button reads **Start anyway**): a phone below 90 Hz of real
data; phones on different app versions; unknown clock offset; a camera that has drifted from
the active calibration reference; disk under 20 GB. Starting with warnings writes them into
the session's audit record and the CSV metadata line as `preflight_failed=…`, so the
recording documents its own condition — that is not a licence to ignore them.

Pay particular attention to **sampling rate**. Two devices silently recorded at ~50 Hz while
claiming 100 Hz on 2026-08-07, and half of every one of those files is interpolated padding.
The number on the card is *distinct readings per second*, not packets.

If a camera has moved since its reference: take fresh **reference stills** (one button,
all live cameras at the same instant) and check the overlay in **Calibration**.

## 6. Dry run (2 minutes, no subject)

1. Select the cameras, take reference stills, open Calibration: every camera should read
   **OK**.
2. Fill Subject = `TEST`, Tag = `dryrun`, press **Start**, type label `1`, press **Enter**,
   let it run ~20 s, hold **STOP** for a second.
3. Done view: every finalize step green, verdict **PASS**, the ZIP path shown, **Open folder**
   opens it. The ZIP is on disk — **Download ZIP** is never required.
4. Reload the dashboard: the Done view comes back exactly as it was. That is the point of
   the rebuild — nothing lives in the browser.
5. Optional: `python tools/analyze_session.py <session folder>` for rows, gaps and the true
   per-device rate.

Then start the real session. Confirm the top-bar phone count against the number of phones on
the subject.

---

## If something goes wrong at STOP

**Do not re-record, and do not delete anything.** The data is already on disk:

1. **Backend session folder** — `<SSD_PATH>/Data_Riset_IMU/<subject>_<tag>/`, CSVs fsync'd
   every `FSYNC_INTERVAL_SEC` and closed before the integrity report is written.
2. **The session ZIP** — `<subject>_<tag>_<sid>.zip` in the same folder, written by the
   backend after finalize; downloading it is optional.
3. **Each phone's rescue CSV** — written for the whole session regardless of the network,
   at `/sdcard/Android/data/com.example.sensors_app/files/imu_sessions/`.

Recovery moves, in order:

- Crashed browser? **Reload it.** The Done view is driven by the backend and comes back with
  the same progress; the ZIP is already on disk.
- ZIP missing but the session folder is complete? Rebuild it without the browser:
  ```bash
  curl -X POST http://<laptop>:8000/api/sessions/<sid>/package
  ```
- Phone still holding un-uploaded data? Open the app (it retries automatically) or pull it
  and hand it over; the Done view lists outstanding uploads, and late data inside
  `LATE_ACCEPT_SEC` is merged and the ZIP rebuilt.
- Verify before analysis: `python tools/analyze_session.py <session folder>`.