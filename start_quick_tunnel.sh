#!/bin/bash
# Start Cloudflare Quick Tunnel in the background (no shell needed).
# Updates HTPC_ENV + CURRENT_ADDON_URL.txt, then restarts srt-streamio.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

CF="./cloudflared"
LOCAL_PORT="${STREAMIO_PORT:-${STREMIO_PORT:-5055}}"
LOG="./.quick_tunnel.log"
PIDFILE="./.quick_tunnel.pid"
OUT="./quick_tunnel.out"

[[ -x "$CF" ]] || { echo "cloudflared not found at $CF"; exit 1; }

stop_tunnel() {
  if [[ -f "$PIDFILE" ]]; then
    local pid
    pid="$(cat "$PIDFILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      sleep 1
    fi
    rm -f "$PIDFILE"
  fi
  pkill -f "cloudflared tunnel --no-autoupdate --url http://localhost:${LOCAL_PORT}" 2>/dev/null || true
}

echo ">> Stopping any existing quick tunnel ..."
stop_tunnel

echo ">> Ensuring addon is listening on http://127.0.0.1:${LOCAL_PORT} ..."
if ! curl -fsS -m 3 "http://127.0.0.1:${LOCAL_PORT}/healthz" >/dev/null 2>&1; then
  echo ">> Addon not up; starting container first ..."
  NO_LOGS=1 echo "${SUDO_PASSWORD:-admin}" | sudo -S env NO_LOGS=1 ./run_streamio.sh
fi

echo ">> Starting Cloudflare Quick Tunnel (background) ..."
: > "$LOG"
: > "$OUT"
nohup "$CF" tunnel --no-autoupdate --url "http://127.0.0.1:${LOCAL_PORT}" >>"$OUT" 2>&1 &
TUNNEL_PID=$!
echo "$TUNNEL_PID" >"$PIDFILE"

URL=""
for _ in $(seq 1 45); do
  URL="$(grep -ohE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" "$OUT" 2>/dev/null | head -1 || true)"
  [[ -n "$URL" ]] && break
  if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
    echo "ERROR: tunnel exited early. Log:" >&2
    tail -30 "$LOG" "$OUT" >&2 || true
    exit 1
  fi
  sleep 1
done
[[ -n "$URL" ]] || { echo "ERROR: could not detect trycloudflare URL" >&2; tail -30 "$LOG" "$OUT" >&2 || true; exit 1; }

MANIFEST="${URL}/manifest.json"
echo ">> Public URL: $URL"

if grep -q '^export STREAMIO_PUBLIC_BASE_URL=' ./HTPC_ENV; then
  sed -i "s#^export STREAMIO_PUBLIC_BASE_URL=.*#export STREAMIO_PUBLIC_BASE_URL='${URL}'#" ./HTPC_ENV
elif grep -q '^export STREMIO_PUBLIC_BASE_URL=' ./HTPC_ENV; then
  sed -i "s#^export STREMIO_PUBLIC_BASE_URL=.*#export STREAMIO_PUBLIC_BASE_URL='${URL}'#" ./HTPC_ENV
else
  echo "export STREAMIO_PUBLIC_BASE_URL='${URL}'" >> ./HTPC_ENV
fi

cat > ./CURRENT_ADDON_URL.txt <<EOF
Stremio -> Addons (puzzle icon) -> paste URL -> Install:

${MANIFEST}
EOF

echo ">> Wrote CURRENT_ADDON_URL.txt and HTPC_ENV"
echo ">> Restarting srt-streamio with new public URL ..."
NO_LOGS=1 echo "${SUDO_PASSWORD:-admin}" | sudo -S env NO_LOGS=1 ./run_streamio.sh

echo "================================================"
echo "Tunnel PID: $TUNNEL_PID (background)"
echo "Stremio addon URL:"
echo "  ${MANIFEST}"
echo "Saved in: ${ROOT}/CURRENT_ADDON_URL.txt"
echo "Stop tunnel: ./stop_quick_tunnel.sh"
echo "================================================"
