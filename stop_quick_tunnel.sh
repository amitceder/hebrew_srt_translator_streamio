#!/bin/bash
# Stop the background Cloudflare Quick Tunnel.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

LOCAL_PORT="${STREAMIO_PORT:-${STREMIO_PORT:-5055}}"
PIDFILE="./.quick_tunnel.pid"

if [[ -f "$PIDFILE" ]]; then
  pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    echo "Stopped quick tunnel (pid $pid)"
  else
    echo "No running tunnel for pid ${pid:-?}"
  fi
  rm -f "$PIDFILE"
else
  echo "No pid file; killing any matching cloudflared ..."
fi

pkill -f "cloudflared tunnel --no-autoupdate --url http://127.0.0.1:${LOCAL_PORT}" 2>/dev/null || true
pkill -f "cloudflared tunnel --no-autoupdate --url http://localhost:${LOCAL_PORT}" 2>/dev/null || true

echo "Quick tunnel stopped."
