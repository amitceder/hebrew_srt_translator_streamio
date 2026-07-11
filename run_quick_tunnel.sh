#!/bin/bash
# FREE Cloudflare Quick Tunnel for the Streamio addon (no domain needed).
# It:
#   1) starts a trycloudflare.com tunnel to the local addon port
#   2) captures the random public HTTPS URL
#   3) writes it into HTPC_ENV as STREAMIO_PUBLIC_BASE_URL
#   4) (re)starts ONLY the srt-streamio container with that URL
#   5) keeps the tunnel running in the foreground (Ctrl+C stops the tunnel)
#
# NOTE: the URL changes every time you run this. After it prints the manifest
# URL, you must (re)add that manifest in Stremio.
set -euo pipefail

CF="./cloudflared"
LOCAL_PORT="${STREAMIO_PORT:-${STREMIO_PORT:-5055}}"
LOG="./.quick_tunnel.log"

[[ -x "$CF" ]] || { echo "cloudflared not found at $CF"; exit 1; }

echo ">> Starting Cloudflare Quick Tunnel to http://localhost:${LOCAL_PORT} ..."
: > "$LOG"
"$CF" tunnel --no-autoupdate --url "http://localhost:${LOCAL_PORT}" >"$LOG" 2>&1 &
TUNNEL_PID=$!
trap 'echo; echo ">> Stopping tunnel (pid $TUNNEL_PID)"; kill "$TUNNEL_PID" 2>/dev/null || true' EXIT INT TERM

# Wait for the public URL to appear (up to ~40s).
URL=""
for _ in $(seq 1 40); do
  URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1 || true)"
  [[ -n "$URL" ]] && break
  if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
    echo "ERROR: tunnel exited early. Log:"; cat "$LOG"; exit 1
  fi
  sleep 1
done
[[ -n "$URL" ]] || { echo "ERROR: could not detect trycloudflare URL. Log:"; cat "$LOG"; exit 1; }
echo ">> Public URL: $URL"

# Persist into HTPC_ENV.
if grep -q '^export STREAMIO_PUBLIC_BASE_URL=' ./HTPC_ENV; then
  sed -i "s#^export STREAMIO_PUBLIC_BASE_URL=.*#export STREAMIO_PUBLIC_BASE_URL='${URL}'#" ./HTPC_ENV
elif grep -q '^export STREMIO_PUBLIC_BASE_URL=' ./HTPC_ENV; then
  sed -i "s#^export STREMIO_PUBLIC_BASE_URL=.*#export STREAMIO_PUBLIC_BASE_URL='${URL}'#" ./HTPC_ENV
else
  echo "export STREAMIO_PUBLIC_BASE_URL='${URL}'" >> ./HTPC_ENV
fi
echo "$URL" > ./CURRENT_ADDON_URL.txt
echo ">> Wrote STREAMIO_PUBLIC_BASE_URL into HTPC_ENV"

# (Re)start ONLY the Streamio container with the new URL (no log tail).
echo ">> (Re)starting srt-streamio container ..."
NO_LOGS=1 ./run_streamio.sh

echo "================================================"
echo "ADD THIS IN STREMIO (Addons -> paste URL):"
echo "    ${URL}/manifest.json"
echo "Health check:"
echo "    curl -fsS ${URL}/healthz"
echo "Tunnel is running. Press Ctrl+C here to stop it (URL will die)."
echo "================================================"

# Keep the tunnel in the foreground.
wait "$TUNNEL_PID"
