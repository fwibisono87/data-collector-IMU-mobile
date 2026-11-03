# Redmi Note 12 (MIUI / HyperOS) — Per-Phone Setup Checklist

**Why this exists:** MIUI/HyperOS aggressively freezes or kills background apps —
including active foreground services — to save power, especially when the screen is
off. On the Redmi Note 12 this is the primary cause of mid-session disconnects, not an
app bug. The IMU Node app already survives drops (it keeps a rescue CSV on the phone,
buffers to disk, auto-reconnects, and the backend accepts the replayed packets —
**no data is lost**), but a phone that's never been exempted from these OS behaviours
will drop far more often than necessary, which means more alarms, more buffered replay
and a more likely PARTIAL verdict.

Do this **once per phone**, after installing/reinstalling the app, and re-check it after
a system update (`adb install -r` keeps the app's data, but MIUI sometimes resets its
power permissions).

---

## 1. Install the app (native version)

The app is native Kotlin now; build and install from Linux:

```bash
make apk                     # cd mobile_node && ./gradlew assembleRelease
./ops/install_phones.sh      # installs on every adb device, never uninstalls
```

or by hand: `adb install -r mobile_node/app/build/outputs/apk/release/app-release.apk`.

Builds use the dedicated release key from `mobile_node/keystore.properties` (create it
once: `keytool -genkeypair -v -keystore imu-release.jks -alias imu -keyalg RSA -keysize 4096 -validity 36500`;
format in `mobile_node/keystore.properties.example`). Back the `.jks` up: from now on every
field build must be signed with the same key, or the phone needs another uninstall.

The `applicationId` is `id.ac.ui.cs.csl.fams.sapper`. Later builds signed with the same key
install **over** it and keep the phone's `device_id`, checkpoint and buffered packets. Do
**not** uninstall: Android deletes
`/sdcard/Android/data/id.ac.ui.cs.csl.fams.sapper/files/imu_sessions` (the rescue CSVs) on
uninstall.

**Old app (`com.example.sensors_app`):** the first `sapper` install sits *alongside* the old
Flutter/debug-signed app instead of replacing it, so nothing is lost by installing. The new
app gets a fresh `device_id` — the phone looks like a new device to the study — and its MIUI
settings below must be applied to the new app. Remove the old app only after its rescue data
is pulled:

```bash
adb -s <serial> pull /sdcard/Android/data/com.example.sensors_app/files/imu_sessions ./local_backup_<serial>
adb -s <serial> uninstall com.example.sensors_app
```

## 2. First launch — permissions

IMU Node asks for two runtime permissions on first start; accept both:

- **Notifications** (Android 13+) — the foreground-service notification is how you see
  that recording is alive, and it carries the alarm's **SILENCE** action. Without it the
  offline alarm is silent (the Status screen says so: *"Notifications are off — you will
  not see the offline alarm"*).
- **Physical activity** (`ACTIVITY_RECOGNITION`, Android 10+) — a runtime prerequisite of
  the `health` foreground-service type on Android 14+. The service still records if it is
  denied, but grant it.

Discovery uses **mDNS/NSD** with a Wi-Fi multicast lock. That needs no runtime permission
dialog; there is no Wi-Fi scan, so the app does not ask for "Nearby devices". If a phone
still cannot discover the laptop, type the LAN IP shown by the starter — and if your MIUI
build has a "Local network" / multicast switch under the app's permissions, allow it.

The app does **not** auto-start on boot (deliberately): after a reboot, open it once; the
service then survives on its own.

## 3. Autostart — ON
Settings → Apps → Manage apps → **IMU Node** → **Autostart** = on.

## 4. Battery saver — No restrictions
Settings → Apps → Manage apps → **IMU Node** → **Battery saver** → **No restrictions**.

## 5. Battery-optimisation exemption
Settings → Battery → App battery saver → **IMU Node** → **Don't optimize**.

The app also asks for this: on the Status screen a warning *"Battery optimisation can kill
the service"* appears with a **Fix** button that opens the system dialog
(`ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`). Accept it, then confirm the warning is
gone. This step is the one MIUI resets most often.

## 6. Lock the app in Recents
Open Recents (square button / swipe-up-hold) → find the **IMU Node** card → swipe down
on it → tap the **padlock** icon. This stops MIUI's memory cleaner from swiping it away.
The service itself is `stopWithTask=false`, so even without the padlock a swipe does not
end a recording — the padlock keeps the UI (and the alarm's SILENCE button) reachable.

## 7. Keep Wi-Fi on during sleep
Settings → Wi-Fi → Additional settings → **Keep Wi-Fi on during sleep** = **Always**.
This prevents Doze from dropping the Wi-Fi radio while the screen is off.

## 8. Notifications — ON
Settings → Apps → Manage apps → **IMU Node** → Notifications = on. A visible
"IMU Telemetry" notification means the foreground service is alive; MIUI is more likely to
kill a service whose notification is suppressed. The Status screen warns when notifications
are off.

## 9. Disable aggressive memory cleanup (if present)
Settings → Battery → check for "Memory extension" / "Boost speed" style features and
disable for this app, or system-wide if your MIUI version doesn't allow a per-app
exception.

## 10. Field fallback (if a specific unit still drops)
Some individual units are worse offenders even with all settings applied. If so:
record with the **screen on** (dim brightness) or the phone on a **power bank** —
either physically prevents Doze from engaging.

---

## Verification (30 seconds)

1. Apply steps 2–9 above.
2. Open the app, connect, and start a session from the dashboard.
3. The Status screen should show a green (recording/connected) block and **no** amber
   readiness warnings (battery %, battery optimisation, notifications, sampling ≥ 90 Hz,
   clock synced).
4. Turn the phone's screen **off** and leave it untouched for **5 minutes**.
5. Confirm:
   - The dashboard keeps this device **online** the whole time (Record view → phone strip).
   - The "IMU Telemetry" notification is still present when you wake the screen.
   - The recorded CSV has no gap across that window, and the phone card's true rate stays
     at/above 90 Hz.

If it still drops after all 9 steps, use the field fallback (step 10) and note the unit
so it can be swapped or re-checked.

## Data recovery

Every session is also written to phone-local storage from START to STOP, regardless of
Wi-Fi state:

```
/sdcard/Android/data/id.ac.ui.cs.csl.fams.sapper/files/imu_sessions/
  <sid>_<tag>_<subject>_<role>_<devid>_rescue.csv
  <csv>.events.jsonl          service start/restart, connection lost/restored, buffer events
  <csv>.integrity.json        written when the session closes
```

The backend's own copy of the same session is
`<SSD_PATH>/Data_Riset_IMU/<subject>_<tag>/<sid>_<role>_sensor_data.csv` (plus
`…_sensor_data_late.csv` for rows that arrived after STOP).

Pull the phone copy with:

```
adb pull /sdcard/Android/data/id.ac.ui.cs.csl.fams.sapper/files/imu_sessions ./local_backup
```

or over USB/MTP: **Internal storage → android → data → id.ac.ui.cs.csl.fams.sapper → files →
imu_sessions**. No root or special permission is needed — this is app-private external
storage. The app keeps the last 20 sessions (~25 MB/hour each).

You normally do **not** need this file: while connected, the phone uploads its rescue CSV
itself (`/upload/csv`), the Done view shows the upload as a row ("rescue upload … verified
✓"), and the session ZIP is rebuilt automatically. Use the manual pull only when the phone
is gone, wiped, or the dashboard reports an outstanding upload that never completes.

If the dashboard reports a **PARTIAL** integrity status for a session, merge in this order:
1. `<sid>_<role>_sensor_data.csv` (the primary backend capture)
2. `<sid>_<role>_sensor_data_late.csv` (buffered rows the phone delivered within
   `LATE_ACCEPT_SEC`, default 10 minutes), if present
3. Sort the combined rows by `timestamp_ms` and drop duplicate `(device_id,
   sequence_number)` pairs
4. If rows are still missing from that window, pull them from the phone-local rescue CSV
   above — it is the only copy guaranteed to be complete regardless of what happened to
   Wi-Fi.

Note: the phone-local CSV's `label_id`/`label_name` reflect the last label the phone
received over the network. If the phone was offline when the operator changed the label,
those columns may be stale — the backend's primary CSV is authoritative for labels, because
labels are applied server-side.

### About `orphan_*.bin` files

If the app's own "Local files" list ever shows an `orphan_*.bin` file, it means the
network-buffer flush could not confirm which session those bytes belonged to and
quarantined them rather than deleting them. **The phone-local rescue CSV above is already
the recovery path** — orphan files hold the same packets, so you normally don't need them.
They exist for forensics, and for the rare case of a phone that recorded before the local
CSV existed. To read one, use `tools/decode_fallback_buffer.py <file> --device-role <role>
-o out.csv` from the repo root.

## Related

- [`docs/rebuild/PHONE.md`](rebuild/PHONE.md) — the phone contract: wire protocol, file
  formats, the state machine these settings are protecting.
- [`../mobile_node/ARCHITECTURE.md`](../mobile_node/ARCHITECTURE.md) — what runs on the
  phone and which files it keeps.
- [`history/connectivity_ops_fixes_plan.md`](history/connectivity_ops_fixes_plan.md) — **historical** (pre-v3): the full analysis of why
  mid-session drops are ~80 % phone / 20 % app hygiene, and the screen-off test used to
  confirm the cause on a given unit. The OS settings above still apply unchanged.