# Lane L7b: launchers, docs and removal of the old stack

The new system is on `make-proper`:
- the dashboard is a static build served by the backend at `http://<laptop>:8000/`;
- the phone app is native Kotlin in `mobile_node/`;
- cameras are captured by the backend with ffmpeg.

The launch scripts and docs still describe the old stack: `npm install`/`build`/`start` of the
Next.js dashboard on port 3000, `flutter build`, and browser-recorded video. Read
`docs/rebuild/SPEC.md` (§4 and §5) first. Also read `master_backend/ARCHITECTURE.md`,
`master_backend/app/camera/README.md`, `dashboard/README.md` and `mobile_node/ARCHITECTURE.md`;
those four are the truth about the new system.

## Hard rules
- Do not touch `master_backend/app/`, `master_backend/tests/`, `dashboard/` or `mobile_node/`
  (other work is landing there). Only the files listed below, plus deleting
  `master_frontend/`.
- The golden contract suite must still pass:
  `master_backend/.venv/bin/python -m pytest master_backend/tests -q -m "not slow"`.
- The field laptop runs **Windows** and the PowerShell scripts are what operators use. Keep them
  PowerShell 5.1 compatible (no `??`, no `&&` chains, no ternaries). You cannot run PowerShell
  here, so read your scripts twice; if `pwsh` exists on PATH, run
  `pwsh -NoProfile -Command "& { [ScriptBlock]::Create((Get-Content <file> -Raw)) | Out-Null }"`
  to at least parse-check them.

## 1. Delete the old dashboard
- Remove `master_frontend/` entirely: `git rm -r`, and also delete untracked leftovers such as
  node_modules and .next.
- Remove its entries from `.gitignore` if they no longer apply.
- Check nothing else references it (grep for `master_frontend`). Leave references in the files
  listed under "Do not touch" for the project lead and list them in your report.

## 2. Launchers
Rewrite for the new stack. There is no Node at runtime and no port 3000; the dashboard is
`http://<laptop-ip>:8000/`.

**`start_all.ps1`** (the operator's one-click start on Windows) must:
- keep the existing robustness the old script had (read it): stop orphaned processes, create or
  repair the venv, `pip install -r master_backend/requirements.txt`;
- check that `ffmpeg` and `ffprobe` are available (PATH or `vendor\ffmpeg\bin`) and fail with a
  clear message and a download hint if not;
- start `master_backend\run.py` with PORT, SSD_PATH and the other existing variables;
- wait for `/health`, print the LAN IP and the dashboard URL, and open the dashboard in the
  default browser;
- print the phone instructions: install the APK and open the app; it finds the laptop
  automatically (mDNS), or type the IP shown above.

**Other launchers:**
- `start.ps1`: same as `start_all.ps1`, minus dependency installation.
- `start.bat`: calls `start_all.ps1`.
- `ops/stop.ps1`: stops the backend AND any `ffmpeg.exe` it spawned. Match by command line
  containing `master_backend`, never kill unrelated ffmpeg.
- `ops/ip.ps1`: keep, with updated wording.
- `start_all.sh`: the Linux equivalent of `start_all.ps1`.
- `Makefile`: targets `backend`, `test` (backend fast suite + `dashboard npm test`),
  `test-slow`, `dashboard` (npm ci && npm run build), `apk`
  (`cd mobile_node && ./gradlew assembleRelease`) and `install-phones`
  (`ops/install_phones.sh`). Remove frontend/flutter targets.
- `ops/install_phones.sh`: already updated by the phone lane. Verify the APK path matches
  `mobile_node/app/build/outputs/apk/release/app-release.apk`.

## 3. Docs (rewrite, do not append)
- **`README.md`**: what the system is, the new architecture diagram (phones → backend ← cameras;
  dashboard served by the backend), quick start, links to the four component docs and
  `docs/rebuild/SPEC.md`. Keep the language consistent with the current README (Indonesian
  prose is fine where it already is; technical terms stay English).
- **`STARTUP.md`**:
  - first-time setup on Windows: Python 3.12, ffmpeg, the venv;
  - daily start (`start_all.ps1`);
  - installing the APK on phones (`ops/install_phones.sh` from Linux, or `adb install -r`);
  - Linux dev setup;
  - no `--reload` anywhere (`IMU_DEV_RELOAD=1` is the dev-only switch);
  - the env vars: PORT, SSD_PATH, RESCUE_PATH, RECOVERY_PATH, IMU_CAMERAS, IMU_FAKE_CAMERAS,
    IMU_VIDEO_CODEC, IMU_FFMPEG, IMU_FFPROBE.
- **`docs/DEPLOY.md`**: the pre-recording-day checklist for the new stack:
  - same commit everywhere (the dashboard is built into the backend now, so the old
    stale-bundle section shrinks to "the build id in the dashboard top bar must match
    `/health`");
  - phones on the same app version (shown on each phone card);
  - ffmpeg present.
- **`docs/FIELD_CHECKLIST.md`**: update every item for the new flow:
  - Setup readiness list;
  - camera reference stills + calibration check before each subject;
  - Record view with keyboard labels and hold-to-stop;
  - Done view (the ZIP is already on disk; Open folder);
  - "If the dashboard crashes": just reload it, because nothing lives in the browser;
  - where data lives on the laptop and on each phone.

  Keep the incident history that explains WHY each item exists, wherever it still applies.
- **`docs/REDMI_NOTE_12_SETUP.md`**: keep the OEM battery/autostart guidance, and update the app
  install and permission steps for the native app (notification permission, battery
  optimisation exemption, nearby-devices/multicast for discovery if applicable).
- Remove or update obsolete root planning docs only if they claim to describe the current
  system (`connectivity_ops_fixes_plan.md`, `mul_cam_plan_audit.md`, `DOKUMENTASI.md`). Add a
  one-line banner at the top of each, "Historical — describes the pre-v3 system; see
  docs/rebuild/SPEC.md". Do not delete them.

## Done
- `git grep -n "master_frontend\|npm run start\|:3000\|flutter"` finds nothing outside the
  historical docs, `docs/rebuild/`, `docs/OUTPUT_CONTRACT.md` (which documents the old
  browser ZIP on purpose) and the "Do not touch" directories.
- The backend fast suite passes.
- Commit in logical steps with `git commit --no-gpg-sign -m "..."`. Messages explain why, with
  no trailers.
- Final report: what changed per file, the PowerShell parse-check results (or that pwsh was
  unavailable), and leftover references in the do-not-touch dirs.
