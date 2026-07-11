#!/bin/bash
# One-time Cloudflare NAMED tunnel setup for the Streamio addon.
# Usage:  ./setup_tunnel.sh hebrew-subs.yourdomain.com
# Requires: a domain already added to your Cloudflare account.
set -euo pipefail

HOSTNAME_ARG="${1:?Usage: ./setup_tunnel.sh <hostname e.g. hebrew-subs.yourdomain.com>}"
TUNNEL_NAME="srt-streamio"
LOCAL_PORT="${STREAMIO_PORT:-${STREMIO_PORT:-5055}}"
CF="./cloudflared"
CF_DIR="$HOME/.cloudflared"
CONFIG="./cloudflared-config.yml"
EXPECTED_ZONE="${CLOUDFLARE_ZONE:-streamio-amit.com}"

[[ -x "$CF" ]] || { echo "cloudflared binary not found at $CF"; exit 1; }

if [[ "$HOSTNAME_ARG" != *".${EXPECTED_ZONE}" && "$HOSTNAME_ARG" != "$EXPECTED_ZONE" ]]; then
  echo "ERROR: Hostname '$HOSTNAME_ARG' is not under Cloudflare zone '$EXPECTED_ZONE'." >&2
  echo "Use something like: hebrew-subs.${EXPECTED_ZONE}" >&2
  echo "Or override with: CLOUDFLARE_ZONE=yourdomain.com ./setup_tunnel.sh subs.yourdomain.com" >&2
  exit 1
fi

# 1) Authenticate (opens a browser / prints a URL). Skipped if cert already exists.
if [[ ! -f "$CF_DIR/cert.pem" ]]; then
  echo ">> Logging in to Cloudflare (a browser/URL will appear; pick your domain)..."
  "$CF" tunnel login
fi

# 2) Create the tunnel if it does not exist yet.
if ! "$CF" tunnel list 2>/dev/null | awk '{print $2}' | grep -qx "$TUNNEL_NAME"; then
  echo ">> Creating tunnel $TUNNEL_NAME..."
  "$CF" tunnel create "$TUNNEL_NAME"
fi

# 3) Find the tunnel ID + credentials file.
TUNNEL_ID="$("$CF" tunnel list 2>/dev/null | awk -v n="$TUNNEL_NAME" '$2==n {print $1}' | head -1)"
CRED_FILE="$CF_DIR/$TUNNEL_ID.json"
[[ -f "$CRED_FILE" ]] || { echo "Credentials file not found: $CRED_FILE"; exit 1; }

# 4) Write the ingress config mapping the hostname -> local addon port.
cat > "$CONFIG" <<EOF
tunnel: $TUNNEL_ID
credentials-file: $CRED_FILE
ingress:
  - hostname: $HOSTNAME_ARG
    service: http://localhost:$LOCAL_PORT
  - service: http_status:404
EOF
echo ">> Wrote $CONFIG"

# 5) Point DNS (CNAME) at the tunnel.
echo ">> Routing DNS $HOSTNAME_ARG -> $TUNNEL_NAME..."
"$CF" tunnel route dns "$TUNNEL_NAME" "$HOSTNAME_ARG"

# 6) Persist the public URL into HTPC_ENV so the container advertises correct URLs.
if [[ -f ./HTPC_ENV ]]; then
  if grep -q '^export STREAMIO_PUBLIC_BASE_URL=' ./HTPC_ENV; then
    sed -i "s#^export STREAMIO_PUBLIC_BASE_URL=.*#export STREAMIO_PUBLIC_BASE_URL='https://$HOSTNAME_ARG'#" ./HTPC_ENV
  elif grep -q '^export STREMIO_PUBLIC_BASE_URL=' ./HTPC_ENV; then
    sed -i "s#^export STREMIO_PUBLIC_BASE_URL=.*#export STREAMIO_PUBLIC_BASE_URL='https://$HOSTNAME_ARG'#" ./HTPC_ENV
  else
    echo "export STREAMIO_PUBLIC_BASE_URL='https://$HOSTNAME_ARG'" >> ./HTPC_ENV
  fi
  echo ">> Set STREAMIO_PUBLIC_BASE_URL=https://$HOSTNAME_ARG in HTPC_ENV"
fi

echo "------------------------------------------------"
echo "Tunnel ready. Next:"
echo "  1) (re)start the addon container:   sudo ./run_streamio.sh"
echo "  2) start the tunnel:                ./run_tunnel.sh"
echo "  3) verify:  curl -fsS https://$HOSTNAME_ARG/healthz"
echo "     manifest: https://$HOSTNAME_ARG/manifest.json"
echo "------------------------------------------------"
