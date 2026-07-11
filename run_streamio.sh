#!/bin/bash
# Dedicated launcher for the Streamio addon container.
# Kept fully separate from the local translator container (srt-app):
#   - separate image tag:      srt-streamio-addon
#   - separate container name:  srt-streamio
#   - separate host port:       ${STREAMIO_PORT:-5055} -> 5000 (bridge, not host net)
# This script NEVER stops/removes/builds the local srt-app container or its image.
set -euo pipefail

IMAGE_NAME="srt-streamio-addon"
CONTAINER_NAME="srt-streamio"
HOST_PORT="${STREAMIO_PORT:-${STREMIO_PORT:-5055}}"

DOCKER="docker"
docker info >/dev/null 2>&1 || DOCKER="sudo docker"

# Load private env (secrets + public URL). Never commit HTPC_ENV.
if [[ -f ./HTPC_ENV ]]; then
  set -a; source ./HTPC_ENV; set +a
fi

# Backward compatibility for older HTPC_ENV variable names.
STREAMIO_PUBLIC_BASE_URL="${STREAMIO_PUBLIC_BASE_URL:-${STREMIO_PUBLIC_BASE_URL:-}}"
STREAMIO_TOKEN_SECRET="${STREAMIO_TOKEN_SECRET:-${STREMIO_TOKEN_SECRET:-}}"

# Safety guard: this script must never operate on the local translator.
if [[ "$CONTAINER_NAME" == "srt-app" || "$IMAGE_NAME" == "hybrid-srt-translator" ]]; then
  echo "Refusing to run: names collide with the local translator container." >&2
  exit 1
fi

# Required configuration checks.
missing=0
if [[ -z "${GROQ_API_KEY:-}" || "${GROQ_API_KEY}" == "replace-me" ]]; then
  echo "ERROR: GROQ_API_KEY is not set in HTPC_ENV" >&2; missing=1
fi
if [[ -z "${OPEN_SUBTITLES_API_KEY:-}" || "${OPEN_SUBTITLES_API_KEY}" == "replace-me" ]]; then
  echo "ERROR: OPEN_SUBTITLES_API_KEY is not set in HTPC_ENV (addon cannot fetch source subs)" >&2; missing=1
fi
if [[ -z "${STREAMIO_PUBLIC_BASE_URL:-}" || "${STREAMIO_PUBLIC_BASE_URL}" == "https://your-stable-public-host" ]]; then
  echo "ERROR: STREAMIO_PUBLIC_BASE_URL is not set in HTPC_ENV (must be a public HTTPS URL)" >&2; missing=1
fi
if [[ "$missing" -ne 0 ]]; then
  echo "Fill in HTPC_ENV and re-run. Aborting." >&2
  exit 1
fi

OPEN_SUBTITLES_USER_AGENT="${OPEN_SUBTITLES_USER_AGENT:-HebrewAIStreamioAddon v0.1}"
OPEN_SUBTITLES_LANGUAGES="${OPEN_SUBTITLES_LANGUAGES:-en,ar,hr,el,tr}"
STREAMIO_PRIMARY_SOURCE_LANGUAGES="${STREAMIO_PRIMARY_SOURCE_LANGUAGES:-en,ar}"

echo "Step 1: Building Streamio image ($IMAGE_NAME)..."
$DOCKER build -t "$IMAGE_NAME" .

echo "Step 2: Cleaning up ONLY the Streamio container ($CONTAINER_NAME)..."
$DOCKER stop "$CONTAINER_NAME" >/dev/null 2>&1 || true
$DOCKER rm "$CONTAINER_NAME" >/dev/null 2>&1 || true

echo "Step 3: Launching Streamio addon on bridge port ${HOST_PORT}..."
$DOCKER run -d \
  -p "${HOST_PORT}:5000" \
  -e GROQ_API_KEY="$GROQ_API_KEY" \
  -e GROQ_MODEL="${GROQ_MODEL:-llama-3.1-8b-instant}" \
  -e AI_PARALLEL_WORKERS="${AI_PARALLEL_WORKERS:-2}" \
  -e AI_BATCH_SIZE="${AI_BATCH_SIZE:-8}" \
  -e AI_MAX_TOKENS="${AI_MAX_TOKENS:-900}" \
  -e OPEN_SUBTITLES_API_KEY="$OPEN_SUBTITLES_API_KEY" \
  -e OPEN_SUBTITLES_USERNAME="${OPEN_SUBTITLES_USERNAME:-}" \
  -e OPEN_SUBTITLES_PASSWORD="${OPEN_SUBTITLES_PASSWORD:-}" \
  -e OPEN_SUBTITLES_USER_AGENT="$OPEN_SUBTITLES_USER_AGENT" \
  -e OPEN_SUBTITLES_LANGUAGES="$OPEN_SUBTITLES_LANGUAGES" \
  -e STREAMIO_PRIMARY_SOURCE_LANGUAGES="$STREAMIO_PRIMARY_SOURCE_LANGUAGES" \
  -e STREAMIO_MAX_VERIFY_DOWNLOADS="${STREAMIO_MAX_VERIFY_DOWNLOADS:-8}" \
  -e STREAMIO_PUBLIC_BASE_URL="$STREAMIO_PUBLIC_BASE_URL" \
  -e STREMIO_PUBLIC_BASE_URL="$STREAMIO_PUBLIC_BASE_URL" \
  -e STREAMIO_TOKEN_SECRET="$STREAMIO_TOKEN_SECRET" \
  -e STREMIO_TOKEN_SECRET="$STREAMIO_TOKEN_SECRET" \
  -v "$(pwd)/streamio_cache:/app/streamio_cache" \
  -v "$(pwd)/manual_sources:/app/manual_sources" \
  --name "$CONTAINER_NAME" \
  --restart unless-stopped \
  "$IMAGE_NAME"

echo "------------------------------------------------"
echo "Streamio addon container: $CONTAINER_NAME"
echo "Local check:   http://localhost:${HOST_PORT}/healthz"
echo "Manifest:      ${STREAMIO_PUBLIC_BASE_URL}/manifest.json"
echo "Local docker 'srt-app' was NOT touched."
echo "------------------------------------------------"
if [[ "${NO_LOGS:-0}" != "1" ]]; then
  $DOCKER logs -f "$CONTAINER_NAME"
fi
