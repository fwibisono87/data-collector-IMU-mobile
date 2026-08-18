#!/usr/bin/env bash
# Install the IMU Node APK onto every connected phone, without losing local data.
#
# WHY the care around uninstall: the phone-local rescue CSVs — the system's data
# guarantee — live in app-specific external storage
# (/sdcard/Android/data/com.example.sensors_app/files/imu_sessions), which Android
# DELETES on uninstall. So this script never uninstalls. If a signature mismatch makes
# `adb install -r` impossible, it stops and tells you to pull the data off first.
set -uo pipefail

PKG="com.example.sensors_app"
APK="${1:-mobile_node/build/app/outputs/flutter-apk/app-release.apk}"

if [ ! -f "$APK" ]; then
  echo "APK not found: $APK" >&2
  echo "Build it first:  cd mobile_node && flutter build apk --release" >&2
  exit 1
fi

# aapt2 lives in the SDK build-tools, which is usually not on PATH. Resolve it from the
# usual places, then from mobile_node/local.properties, before giving up.
find_aapt2() {
  command -v aapt2 2>/dev/null && return 0
  local roots=("${ANDROID_HOME:-}" "${ANDROID_SDK_ROOT:-}" "$HOME/.android-sdk" "$HOME/Android/Sdk")
  local from_props
  from_props=$(sed -n 's/^sdk\.dir=//p' mobile_node/local.properties 2>/dev/null)
  [ -n "$from_props" ] && roots+=("$from_props")
  for root in "${roots[@]}"; do
    [ -z "$root" ] && continue
    local found
    found=$(ls -1 "$root"/build-tools/*/aapt2 2>/dev/null | sort -V | tail -1)
    [ -n "$found" ] && { echo "$found"; return 0; }
  done
  return 1
}

AAPT2=$(find_aapt2)
WANT_VERSION=""
if [ -n "$AAPT2" ]; then
  WANT_VERSION=$("$AAPT2" dump badging "$APK" 2>/dev/null | sed -n "s/.*versionName='\([^']*\)'.*/\1/p")
fi
if [ -z "$WANT_VERSION" ]; then
  # Fall back to the source of truth rather than reporting nothing.
  WANT_VERSION=$(sed -n 's/^version: *\([0-9.]*\).*/\1/p' mobile_node/pubspec.yaml 2>/dev/null)
fi
[ -z "$WANT_VERSION" ] && WANT_VERSION="(unknown)"
echo "APK: $APK"
echo "     version $WANT_VERSION  sha256 $(sha256sum "$APK" | cut -c1-16)…"
echo

mapfile -t DEVICES < <(adb devices | awk 'NR>1 && $2=="device" {print $1}')
if [ "${#DEVICES[@]}" -eq 0 ]; then
  echo "No authorised devices. Check the USB cable, enable USB debugging, and accept" >&2
  echo "the RSA prompt on the phone (adb devices should show 'device', not 'unauthorized')." >&2
  exit 1
fi
echo "Found ${#DEVICES[@]} device(s): ${DEVICES[*]}"
echo

FAILED=0
for serial in "${DEVICES[@]}"; do
  model=$(adb -s "$serial" shell getprop ro.product.model 2>/dev/null | tr -d '\r')
  installed=$(adb -s "$serial" shell dumpsys package "$PKG" 2>/dev/null \
              | sed -n 's/.*versionName=\([^ ]*\).*/\1/p' | head -1 | tr -d '\r')
  echo "── $serial  ($model)  currently: ${installed:-not installed}"

  # Count what is on the phone before touching anything.
  sessions=$(adb -s "$serial" shell "ls /sdcard/Android/data/$PKG/files/imu_sessions 2>/dev/null | wc -l" 2>/dev/null | tr -d '\r')
  if [ "${sessions:-0}" -gt 0 ]; then
    echo "   holds $sessions local session file(s) — these survive an -r upgrade, not an uninstall"
  fi

  out=$(adb -s "$serial" install -r -d "$APK" 2>&1)
  if echo "$out" | grep -q "Success"; then
    now=$(adb -s "$serial" shell dumpsys package "$PKG" 2>/dev/null \
          | sed -n 's/.*versionName=\([^ ]*\).*/\1/p' | head -1 | tr -d '\r')
    if [ "$now" = "$WANT_VERSION" ]; then
      echo "   ✓ installed, now reporting $now"
    else
      echo "   ! installed but reports '$now', expected '$WANT_VERSION'"
      FAILED=1
    fi
  elif echo "$out" | grep -q "INSTALL_FAILED_UPDATE_INCOMPATIBLE\|signatures do not match"; then
    echo "   ✕ SIGNATURE MISMATCH — this APK was built on a different machine than the"
    echo "     one that produced the installed build. Do NOT uninstall until you have"
    echo "     pulled any un-uploaded sessions:"
    echo "       adb -s $serial pull /sdcard/Android/data/$PKG/files/imu_sessions ./local_backup_$serial"
    echo "     then:  adb -s $serial uninstall $PKG && adb -s $serial install $APK"
    FAILED=1
  else
    echo "   ✕ install failed:"
    echo "$out" | sed 's/^/     /'
    FAILED=1
  fi
  echo
done

if [ "$FAILED" -eq 0 ]; then
  echo "All devices on $WANT_VERSION."
  echo "Confirm on the dashboard: each device card should show app_version $WANT_VERSION."
else
  echo "One or more devices need attention (see above)." >&2
  exit 1
fi
