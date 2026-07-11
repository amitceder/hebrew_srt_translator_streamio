#!/bin/bash
set -euo pipefail

MANIFEST_URL="${STREMIO_MANIFEST_URL:?Set STREMIO_MANIFEST_URL to the public /manifest.json URL}"

curl -fsS \
  -H 'Content-Type: application/json' \
  -d "{\"transportUrl\":\"${MANIFEST_URL}\",\"transportName\":\"http\"}" \
  -X POST https://api.strem.io/api/addonPublish
printf '\nPublished manifest: %s\n' "$MANIFEST_URL"
