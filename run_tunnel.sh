#!/bin/bash
# Runs the named Cloudflare tunnel in the foreground.
# For a permanent background service, install it with:
#   sudo ./cloudflared --config "$(pwd)/cloudflared-config.yml" service install
set -euo pipefail
CF="./cloudflared"
CONFIG="./cloudflared-config.yml"
[[ -f "$CONFIG" ]] || { echo "Run ./setup_tunnel.sh <hostname> first."; exit 1; }
exec "$CF" --config "$CONFIG" tunnel run srt-streamio
