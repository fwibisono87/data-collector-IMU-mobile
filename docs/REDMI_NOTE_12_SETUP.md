# Redmi Note 12 (MIUI / HyperOS) — Per-Phone Setup Checklist

**Why this exists:** MIUI/HyperOS aggressively freezes or kills background apps —
including active foreground services — to save power, especially when the screen is
off. On the Redmi Note 12 this is the primary cause of mid-session disconnects, not an
app bug. The IMU Node app already survives drops (buffers to disk, auto-reconnects,
backend dedups on resend — **no data is lost**), but a phone that's never been
exempted from these OS behaviors will drop far more often than necessary.

Do this **once per phone**, after installing/reinstalling the app.

---

## 1. Autostart — ON
Settings → Apps → Manage apps → **IMU Node** → **Autostart** = on.

## 2. Battery saver — No restrictions
Settings → Apps → Manage apps → **IMU Node** → **Battery saver** → **No restrictions**.

## 3. Battery-optimization exemption
Settings → Battery → App battery saver → **IMU Node** → **Don't optimize**.
(The app also prompts for this automatically on first launch — accept the dialog if
it appears. This step confirms it stuck.)

## 4. Lock the app in Recents
Open Recents (square button / swipe-up-hold) → find the **IMU Node** card → swipe down
on it → tap the **padlock** icon. This stops MIUI's memory cleaner from swiping it away.

## 5. Keep Wi-Fi on during sleep
Settings → Wi-Fi → Additional settings → **Keep Wi-Fi on during sleep** = **Always**.
This prevents Doze from dropping the Wi-Fi radio while the screen is off.

## 6. Notifications — ON
Settings → Apps → Manage apps → **IMU Node** → Notifications = on. (The app also
requests the Android 13 notification permission on first launch — accept it.) A
visible "IMU Telemetry" notification means the foreground service is alive; MIUI is
more likely to kill a service whose notification is suppressed.

## 7. Disable aggressive memory cleanup (if present)
Settings → Battery → check for "Memory extension" / "Boost speed" style features and
disable for this app, or system-wide if your MIUI version doesn't allow a per-app
exception.

## 8. Field fallback (if a specific unit still drops)
Some individual units are worse offenders even with all settings applied. If so:
record with the **screen on** (dim brightness) or the phone on a **power bank** —
either physically prevents Doze from engaging.

---

## Verification (30 seconds)

1. Apply steps 1–7 above.
2. Start a session with this phone attached.
3. Turn the phone's screen **off** and leave it untouched for **5 minutes**.
4. Confirm:
   - The dashboard keeps this device **ONLINE** the whole time.
   - The "IMU Telemetry" notification is still present when you wake the screen.
   - The recorded CSV has no gap across that window.

If it still drops after all 7 steps, use the field fallback (step 8) and note the unit
so it can be swapped or re-checked.

## Data recovery

Every session is also written to phone-local storage from START to STOP, regardless of
Wi-Fi state:

```
/sdcard/Android/data/com.example.sensors_app/files/imu_sessions/<session_id>_<role>.csv
```

Pull it with:

```
adb pull /sdcard/Android/data/com.example.sensors_app/files/imu_sessions ./local_backup
```

or over USB/MTP: **Internal storage → Android → data → com.example.sensors_app → files →
imu_sessions**. No root or special permission is needed — this is app-private external
storage. The app keeps the last 20 sessions (~25 MB/hour each).

If the dashboard reports a **PARTIAL** integrity status for a session, merge data in this
order:
1. `<session_id>_<role>_sensor_data.csv` (the primary backend capture)
2. `<session_id>_<role>_sensor_data_late.csv` (buffered rows the phone delivered within
   10 minutes of STOP), if present
3. Sort the combined rows by `timestamp_ms` and drop duplicate `(device_id,
   sequence_number)` pairs
4. If rows are still missing from that window, pull them from the phone-local CSV above —
   it is the only copy guaranteed to be complete regardless of what happened to Wi-Fi.

Note: the phone-local CSV's `label_id`/`label_name` reflect the last label the phone
received over the network. If the phone was offline when the operator changed the label,
those columns may be stale — the backend's primary CSV is authoritative for labels.

### About `orphan_*.bin` files

If the app's own "Local files" list ever shows an `orphan_*.bin` file, it means the
network-buffer flush could not confirm which session those bytes belonged to and
quarantined them rather than deleting them. **The phone-local CSV above is already the
recovery path** — orphan files hold the same packets, so you normally don't need them.
They exist for forensics, and for the rare case of a phone that recorded before this
local-CSV feature was deployed. To read one, use `tools/decode_fallback_buffer.py
<file> --device-role <role> -o out.csv` from the repo root.

## Related

See `connectivity_ops_fixes_plan.md` §5 for the full analysis (why this is ~80% phone /
20% app-hygiene) and the decisive screen-off test used to confirm the cause on a given
unit.
