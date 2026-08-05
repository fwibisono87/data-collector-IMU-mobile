#!/usr/bin/env bash
# start_all.sh — all-in-one launcher (backend + frontend) for Linux/macOS.
# Starts the backend and frontend in this terminal, waits until the frontend is
# responding, opens it in the default browser, and prints the backend LAN IP that
# the phones connect to. Ctrl+C stops everything (trap cleanup).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$ROOT/master_backend"
FRONTEND_PORT=3000
BACKEND_PORT=8000

BACKEND_PID=""
FRONTEND_PID=""
cleanup() {
  [ -n "$BACKEND_PID" ]  && kill "$BACKEND_PID"  2>/dev/null || true
  [ -n "$FRONTEND_PID" ] && kill "$FRONTEND_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "=== IMU Telemetry — Start All ==="

# --- preflight: .env completeness (fail fast) ---
ENV_FILE="$BACKEND_DIR/.env"
REQUIRED_KEYS=(SSD_PATH RESCUE_PATH BIND_HOST PORT LAN_SUBNET \
               FSYNC_INTERVAL_SEC MAX_CONCURRENT_DEVICES LATE_ACCEPT_SEC SORT_CSV_ON_CLOSE)
check_env() {
  if [ ! -f "$ENV_FILE" ]; then
    echo "FAIL: $ENV_FILE missing — copy $BACKEND_DIR/.env.example to .env and fill it in." >&2
    exit 1
  fi
  local missing=() key
  for key in "${REQUIRED_KEYS[@]}"; do
    if ! grep -Eq "^${key}=.+" "$ENV_FILE"; then
      missing+=("$key")
    fi
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    echo "FAIL: missing required env vars in $ENV_FILE:" >&2
    printf '  - %s\n' "${missing[@]}" >&2
    echo "Fill them in (from $BACKEND_DIR/.env.example), then re-run." >&2
    exit 1
  fi
}
check_env

# The backend LAN IP (this is what the phones type in over Wi-Fi).
BACKEND_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "${BACKEND_IP:-}" ] || BACKEND_IP="127.0.0.1"
echo "Backend IP : $BACKEND_IP  (ws://$BACKEND_IP:$BACKEND_PORT/ ...)"

# --- backend ---
PY="$BACKEND_DIR/venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python || true)"
[ -n "${PY:-}" ] || PY="python"
echo "Backend    : starting ($PY master_backend/run.py) on :$BACKEND_PORT"
"$PY" "$BACKEND_DIR/run.py" &
BACKEND_PID=$!

# --- frontend ---
echo "Frontend   : starting (npm run dev) on :$FRONTEND_PORT"
(
  cd "$FRONTEND_DIR"
  exec npm run dev
) &
FRONTEND_PID=$!

# --- wait for the frontend, then open it in the default browser ---
URL="http://localhost:$FRONTEND_PORT"
echo "Waiting for $URL to respond ..."
for _ in $(seq 1 60); do
  if curl -sf -o /dev/null "$URL"; then break; fi
  sleep 1
done

if curl -sf -o /dev/null "$URL"; then
  echo "Frontend ready: $URL — opening default browser"
  xdg-open "$URL" >/dev/null 2>&1 || open "$URL" >/dev/null 2>&1 || true
else
  echo "WARNING: $URL did not respond in 60s — open it manually."
fi

echo ""
echo "Both running in this terminal. Ctrl+C to stop. Backend IP: $BACKEND_IP"
wait
