#!/usr/bin/env bash
# start_all.sh — the Linux/macOS equivalent of start_all.ps1.
#
# One process tree: master_backend/run.py serves the phones (WebSocket), the cameras
# (one ffmpeg child per camera) and the dashboard (the committed static build) on a
# single port. There is no Node at runtime and no port 3000 — the dashboard is
# http://<laptop-ip>:8000/. This script starts Python and nothing else.
#
# Same guarantees as the Windows script: complete master_backend/.env with its values
# exported (run.py reads PORT before dotenv runs), no stale backend or ffmpeg left
# holding the port or a camera, ffmpeg/ffprobe present, a working venv with the
# requirements installed, then /health before the dashboard URL is printed.
#
# IMU_DEV_RELOAD=1 (in .env or exported) is honoured here because this script is the
# development launcher — never on the field laptop, where a reload mid-recording
# restarts the backend under a running session.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$ROOT/master_backend"
VENV_DIR="$BACKEND_DIR/.venv"
VENV_PY="$VENV_DIR/bin/python"
ENV_FILE="$BACKEND_DIR/.env"
REQUIREMENTS="$BACKEND_DIR/requirements.txt"
VENDOR_DIR="$ROOT/vendor/ffmpeg"
STATIC_INDEX="$BACKEND_DIR/app/static/index.html"

BACKEND_PID=""
cleanup() {
  if [ -n "$BACKEND_PID" ] && kill -0 "$BACKEND_PID" 2>/dev/null; then
    echo ""
    echo "Stopping backend (PID $BACKEND_PID) ..."
    kill -TERM "$BACKEND_PID" 2>/dev/null || true
    # The backend finalises an in-flight session on SIGTERM and then closes the camera
    # service; a session can take a few seconds. Only SIGKILL after that is up.
    for _ in $(seq 1 60); do
      kill -0 "$BACKEND_PID" 2>/dev/null || break
      sleep 0.5
    done
    kill -KILL "$BACKEND_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo "=== IMU Telemetry — Start All ==="

# ---------------------------------------------------------------- .env ---------------
if [ ! -f "$ENV_FILE" ]; then
  echo "FAIL: $ENV_FILE missing — copy $BACKEND_DIR/.env.example to .env and fill it in." >&2
  exit 1
fi

# Read KEY=VALUE ourselves (never `source`: .env is data, and a value with a space or a
# `$` would be executed). Strips quotes and a trailing " # comment", like python-dotenv.
load_env() {
  local line key value
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|\#*) continue ;; esac
    case "$line" in *=*) ;; *) continue ;; esac
    key="${line%%=*}"
    key="${key#"${key%%[![:space:]]*}"}"     # ltrim
    key="${key%"${key##*[![:space:]]}"}"     # rtrim
    case "$key" in
      [A-Za-z_]*)
        case "$key" in *[!A-Za-z0-9_]*) continue ;; esac
        ;;
      *) continue ;;
    esac
    value="${line#*=}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    case "$value" in
      \"*\") value="${value#\"}"; value="${value%\"}" ;;
      \'*\') value="${value#\'}"; value="${value%\'}" ;;
      *)
        # python-dotenv (used by the app) treats " # ..." as a comment; match it so a
        # copy of the documented example (PORT=8000  # ...) does not poison the value.
        value="${value%%[[:space:]]#*}"
        value="${value%"${value##*[![:space:]]}"}"
        ;;
    esac
    export "$key=$value"
  done < "$ENV_FILE"
}
load_env

REQUIRED_KEYS=(SSD_PATH RESCUE_PATH BIND_HOST PORT LAN_SUBNET \
               FSYNC_INTERVAL_SEC MAX_CONCURRENT_DEVICES LATE_ACCEPT_SEC SORT_CSV_ON_CLOSE)
MISSING=()
for key in "${REQUIRED_KEYS[@]}"; do
  if [ -z "${!key:-}" ]; then MISSING+=("$key"); fi
done
if [ "${#MISSING[@]}" -gt 0 ]; then
  echo "FAIL: missing required env vars in $ENV_FILE:" >&2
  printf '  - %s\n' "${MISSING[@]}" >&2
  echo "Fill them in (from $BACKEND_DIR/.env.example), then re-run." >&2
  exit 1
fi

PORT="${PORT:-8000}"
HEALTH_URL="http://127.0.0.1:$PORT/health"

if [ "${IMU_DEV_RELOAD:-0}" = "1" ]; then
  echo "Backend    : IMU_DEV_RELOAD=1 — code changes restart the backend. NEVER on the field laptop." >&2
else
  export IMU_DEV_RELOAD=0
fi

# ---------------------------------------------------------------- LAN IP ------------
# The address that has the default route is the one the phones can reach; `hostname -I`
# happily returns the docker0 bridge first.
get_lan_ip() {
  local ip=""
  if command -v ip >/dev/null 2>&1; then
    ip="$(ip route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -1)"
  fi
  if [ -z "$ip" ] && command -v hostname >/dev/null 2>&1; then
    ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  fi
  [ -n "$ip" ] || ip="127.0.0.1"
  echo "$ip"
}
LAN_IP="$(get_lan_ip)"

# ---------------------------------------------------------------- stop old ----------
port_busy() {
  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | awk 'NR>1 {print $4}' | grep -qE "[:.]${PORT}$"
  elif command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1
  else
    return 1
  fi
}

# Only this checkout's processes: a webcam (or a fake camera) is opened once, and a
# stray ffmpeg keeps it open. Anything else named ffmpeg is somebody's work — leave it.
stop_stale() {
  local pid cmd old_backends=""
  old_backends="$(pgrep -f 'master_backend/run\.py' 2>/dev/null || true)"
  for pid in $old_backends; do
    echo "Backend    : stopping old instance (PID $pid)"
    kill -TERM "$pid" 2>/dev/null || true
  done
  for pid in $(pgrep -x ffmpeg 2>/dev/null || true); do
    cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
    case "$cmd" in
      *Data_Riset_IMU*|*_video_sync*|*"$ROOT/vendor/ffmpeg"*|*"$SSD_PATH"*)
        echo "ffmpeg     : stopping PID $pid  ($(echo "$cmd" | cut -c1-80)…)"
        kill -TERM "$pid" 2>/dev/null || true
        ;;
    esac
  done
  if [ -n "$old_backends" ]; then
    for _ in $(seq 1 40); do
      port_busy || break
      sleep 0.25
    done
  fi
}
stop_stale
if port_busy; then
  echo "FAIL: port $PORT is still taken by a process that is not this checkout's backend." >&2
  if command -v ss >/dev/null 2>&1; then
    ss -ltnp 2>/dev/null | grep -E "[:.]${PORT}\b" >&2 || true
  fi
  echo "      Close it, or change PORT in $ENV_FILE." >&2
  exit 1
fi

# ---------------------------------------------------------------- ffmpeg ------------
resolve_camera_bin() {
  local name="$1" env_name="$2" from_env="" candidate
  from_env="$(printenv "$env_name" 2>/dev/null || true)"
  if [ -n "$from_env" ]; then
    if [ -x "$from_env" ]; then echo "$from_env"; return 0; fi
    echo "WARNING: $env_name points at '$from_env', which is not executable — ignoring it." >&2
  fi
  for candidate in "$VENDOR_DIR/bin/$name" "$VENDOR_DIR/$name"; do
    if [ -x "$candidate" ]; then
      export "$env_name=$candidate"
      echo "$candidate"
      return 0
    fi
  done
  command -v "$name" 2>/dev/null && return 0
  return 1
}

FFMPEG_BIN="$(resolve_camera_bin ffmpeg IMU_FFMPEG || true)"
FFPROBE_BIN="$(resolve_camera_bin ffprobe IMU_FFPROBE || true)"
if [ -z "$FFMPEG_BIN" ] || [ -z "$FFPROBE_BIN" ]; then
  echo "FAIL: ffmpeg / ffprobe not found — the cameras cannot be captured without them." >&2
  [ -z "$FFMPEG_BIN" ] && echo "      Missing: ffmpeg" >&2
  [ -z "$FFPROBE_BIN" ] && echo "      Missing: ffprobe" >&2
  echo "      Fix (pick one):" >&2
  echo "        a) apt install ffmpeg     (Debian/Ubuntu)" >&2
  echo "        b) put ffmpeg + ffprobe in $VENDOR_DIR/bin/" >&2
  echo "        c) set IMU_FFMPEG / IMU_FFPROBE in $ENV_FILE" >&2
  exit 1
fi
echo "ffmpeg     : $FFMPEG_BIN"

if [ ! -f "$STATIC_INDEX" ]; then
  echo "WARNING: no dashboard build at master_backend/app/static/ — http://localhost:$PORT/ will 404." >&2
  echo "         Build it once (needs Node):  make dashboard" >&2
fi

# ---------------------------------------------------------------- python ------------
if ! command -v python3 >/dev/null 2>&1; then
  echo "FAIL: python3 not on PATH (apt install python3 python3-venv)." >&2
  exit 1
fi

deps_ok() {
  "$1" -c 'import fastapi, uvicorn, websockets, google.protobuf, aiofiles, dotenv, zeroconf, paramiko, scipy, numpy, PIL' >/dev/null 2>&1
}

if [ ! -x "$VENV_PY" ]; then
  if [ -d "$VENV_DIR" ]; then
    echo "Backend    : .venv exists but has no bin/python — recreating it ..."
    rm -rf "$VENV_DIR"
  fi
  echo "Backend    : creating venv at $VENV_DIR ..."
  python3 -m venv "$VENV_DIR" || { echo "FAIL: could not create the venv (apt install python3-venv)." >&2; exit 1; }
fi

if ! "$VENV_PY" -c 'import sys' >/dev/null 2>&1; then
  echo "Backend    : the venv's python does not run — rebuilding it ..."
  rm -rf "$VENV_DIR"
  python3 -m venv "$VENV_DIR" || { echo "FAIL: could not recreate the venv." >&2; exit 1; }
fi

if deps_ok "$VENV_PY"; then
  echo "Backend    : dependencies already installed."
else
  echo "Backend    : installing dependencies from requirements.txt ..."
  "$VENV_PY" -m pip install --disable-pip-version-check -r "$REQUIREMENTS" \
    || { echo "FAIL: pip install failed." >&2; exit 1; }
  if ! deps_ok "$VENV_PY"; then
    echo "Backend    : install succeeded but imports still fail — rebuilding the venv from scratch ..." >&2
    rm -rf "$VENV_DIR"
    python3 -m venv "$VENV_DIR" || { echo "FAIL: could not recreate the venv." >&2; exit 1; }
    "$VENV_PY" -m pip install --disable-pip-version-check -r "$REQUIREMENTS" \
      || { echo "FAIL: pip install failed twice. Check the network/proxy." >&2; exit 1; }
  fi
fi

# ---------------------------------------------------------------- run ---------------
echo "Backend    : starting ($VENV_PY master_backend/run.py) on port $PORT"
"$VENV_PY" "$BACKEND_DIR/run.py" &
BACKEND_PID=$!

READY=0
# Generous: the first start creates the mDNS advert and enumerates cameras.
for _ in $(seq 1 90); do
  if ! kill -0 "$BACKEND_PID" 2>/dev/null; then break; fi
  if curl -sf -o /dev/null --max-time 2 "$HEALTH_URL"; then READY=1; break; fi
  sleep 1
done

if [ "$READY" -ne 1 ]; then
  echo "" >&2
  echo "FAIL: the backend did not answer $HEALTH_URL within 90 s." >&2
  if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    wait "$BACKEND_PID"
    echo "      It exited with code $? — the traceback is above." >&2
    BACKEND_PID=""
  else
    echo "      It is still starting or stuck — see the log above." >&2
  fi
  echo "      Common causes: another program on port $PORT, a broken venv (delete" >&2
  echo "      master_backend/.venv and re-run), or an SSD_PATH that does not exist." >&2
  exit 1
fi

BUILD_ID="$(curl -sf --max-time 2 "$HEALTH_URL" \
  | sed -n 's/.*"build_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)"
[ -n "$BUILD_ID" ] || BUILD_ID="unknown"

LOCAL_URL="http://localhost:$PORT/"
LAN_URL="http://$LAN_IP:$PORT/"

echo ""
echo "Backend    : READY  build $BUILD_ID  —  $LOCAL_URL"
echo "Dashboard  : $LAN_URL   (any browser on the Wi-Fi)"
echo "LAN IP     : $LAN_IP"
echo ""
echo "Phones — install the APK once, then just open the app:"
echo "  1. ./ops/install_phones.sh           (or: adb install -r <apk>)"
echo "  2. Open IMU Node. It finds this laptop automatically over mDNS"
echo "     (_imu-telemetry._tcp). If it does not, type  $LAN_IP  in the app."
echo "  3. The Setup screen lists every phone that connected; START needs >= 1 online."
echo ""

if command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$LOCAL_URL" >/dev/null 2>&1 || true
elif command -v open >/dev/null 2>&1; then
  open "$LOCAL_URL" >/dev/null 2>&1 || true
else
  echo "WARNING: no xdg-open/open — open $LOCAL_URL yourself." >&2
fi

echo "Backend running in this terminal. Ctrl+C to stop."
wait "$BACKEND_PID"
STATUS=$?
BACKEND_PID=""
echo ""
echo "Backend exited (code $STATUS)."
exit "$STATUS"