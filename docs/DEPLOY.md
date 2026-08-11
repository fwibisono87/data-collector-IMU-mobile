# Deploy checklist

Run this before every recording day, and after every `git pull`.

It exists because of a specific failure. On **2026-08-11** an operator lost the export of a
17-minute session with a live subject. The backend was flawless — it finalised, retiered the
CSVs, validated and returned to IDLE with zero errors, and all 103k rows per device were on
disk. What failed was the dashboard: the browser was running a **cached JavaScript bundle from
an older deploy** against a newer backend, and it crashed at the moment of saving.

At the time, three machines were on three different commits, and the mobile dependency set
re-resolved per machine. Nothing in the system could notice.

---

## 1. Get every machine onto the same commit

```bash
git pull
git rev-parse --short HEAD      # note this — you will confirm it in step 4
```

Backend host and dashboard host must report the **same sha**. If the dashboard runs on the same
box as the backend, that is one check; if it runs elsewhere, check both.

## 2. Rebuild the frontend — a pull alone is not a deploy

```bash
cd master_frontend
npm ci                          # ci, not install: honours package-lock exactly
npm run build
```

Then **restart** whatever serves it. A running `next start` keeps serving the previous build; the
new chunks exist on disk but nothing is handing them out.

## 3. Restart the backend

```bash
# from the repository root
python master_backend/run.py
```

`GET /health` now reports `build_id` — the backend's actual commit.

## 4. Hard-reload the dashboard and confirm the build matches

Open the dashboard and press **Ctrl+Shift+R** (or DevTools → Network → Disable cache).

In the **PREFLIGHT** panel, confirm:

```
✓ Dashboard build matches backend     <sha>
```

If it is red, the dashboard is running older code than the backend. Click **⟳ Reload dashboard**.
If it stays red after that, step 2 was skipped or the server was not restarted.

> The document is served `no-store` (see `layout.tsx`, `force-dynamic`), so a plain reload is
> enough. `/_next/static/*` is still cached `immutable` — those filenames are content-hashed, so
> caching them hard is correct and desirable.

## 5. Mobile — only when the app changed

```bash
flutter --version               # MUST be 3.44.8
cd mobile_node
flutter pub get                 # pubspec.lock is checked in; do not delete it
flutter analyze                 # expect 0 errors
flutter build apk --release
adb install -r build/app/outputs/flutter-apk/app-release.apk
```

Sanity check the APK before shipping it to the phones: a correct release build is **~52 MB** and
contains `libdartjni.so` for all three ABIs.

```bash
unzip -l build/app/outputs/flutter-apk/app-release.apk | grep -c libdartjni.so   # expect 3
```

A ~22 MB APK missing that library means Flutter resolved the wrong dependency set — you are on
the wrong Flutter version. `install -r` upgrades in place and preserves the phones' local rescue
CSVs; a signature mismatch makes it fail safely rather than wiping anything. **Never `adb
uninstall` a phone that still holds un-uploaded sessions.**

## 6. Before attaching sensors to a person

Check the **PREFLIGHT** panel is all green. It no longer blocks recording — a failing check
warns and the button reads `START ANYWAY (preflight failing)` — so it is on you to read it.

Pay particular attention to **Sampling rate healthy**. Two devices silently recorded at ~50 Hz
while claiming 100 Hz on 2026-08-07, and half of every one of those files is interpolated
padding. If you start anyway, the failing checks are written into the session's audit log and
into the CSV metadata line as `preflight_failed=…`, so the recording documents its own
condition.

---

## If the dashboard crashes at save

**Do not re-record, and do not delete anything.** The data is already safe in three independent
places by the time the export UI draws anything:

1. **Backend CSVs** on the SSD — `data/Data_Riset_IMU/<subject>_<tag>/`, fsync'd every 5 s and
   closed at STOP *before* the integrity report is generated
2. **Each phone's rescue CSV** — written regardless of network, for the whole session
3. **Video chunks** in the browser's IndexedDB — kept until the *next* session starts

Recover it:

- **Data:** click **💾 Save data bundle on backend** in the export dialog, or call it directly —
  it does not need the browser:
  ```bash
  curl -X POST http://<backend-ip>:8000/export/<session_id>/bundle
  ```
  This writes `<session_id>_bundle.zip` next to the session's files on the SSD.
- **Video:** dashboard → **Recover buffered video**.
- **Verify:** `python tools/analyze_session.py <session folder>` reports rows, sequence gaps and
  the true per-device sample rate.
