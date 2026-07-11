#!/bin/bash

# Required secrets are supplied through the environment. Do not store API keys
# in the repository or in todo.md.
IMAGE_NAME="hybrid-srt-translator"
if [[ -z "${GROQ_API_KEY:-}" ]]; then
  echo "Error: Set GROQ_API_KEY in the environment"
  exit 1
fi

if [[ -z "${OPEN_SUBTITLES_API_KEY:-}" ]]; then
  echo "Warning: OPEN_SUBTITLES_API_KEY is not set; the Streamio addon will not resolve source subtitles"
fi

OPEN_SUBTITLES_USER_AGENT="${OPEN_SUBTITLES_USER_AGENT:-HebrewAIStreamioAddon v0.1}"

echo "Step 1: Building Docker Image..."
docker build -t $IMAGE_NAME .

echo "Step 2: Cleaning up old containers..."
docker stop srt-app || true
docker rm srt-app || true

echo "Step 3: Launching Hybrid Translator Server on Host Network..."
# --network host removes the virtual bridge and uses the HTPC's real IP
# Note: -p 5000:5000 is removed because host mode maps all ports automatically
docker run -d \
  --network host \
  -e GROQ_API_KEY="$GROQ_API_KEY" \
  -e OPEN_SUBTITLES_API_KEY="$OPEN_SUBTITLES_API_KEY" \
  -e OPEN_SUBTITLES_USER_AGENT="$OPEN_SUBTITLES_USER_AGENT" \
  -e OPEN_SUBTITLES_LANGUAGES="${OPEN_SUBTITLES_LANGUAGES:-en,ar}" \
  -e STREAMIO_PUBLIC_BASE_URL="${STREAMIO_PUBLIC_BASE_URL:-${STREMIO_PUBLIC_BASE_URL:-}}" \
  -e STREMIO_PUBLIC_BASE_URL="${STREAMIO_PUBLIC_BASE_URL:-${STREMIO_PUBLIC_BASE_URL:-}}" \
  -e STREAMIO_TOKEN_SECRET="${STREAMIO_TOKEN_SECRET:-${STREMIO_TOKEN_SECRET:-}}" \
  -e STREMIO_TOKEN_SECRET="${STREAMIO_TOKEN_SECRET:-${STREMIO_TOKEN_SECRET:-}}" \
  --name srt-app \
  --restart unless-stopped \
  $IMAGE_NAME

echo "------------------------------------------------"
echo "Hybrid Translator is LIVE: http://localhost:5000"
echo "Conflicts with MiniDLNA: RESOLVED ✅"
echo "Opening LIVE LOG TRACKER... (Press Ctrl+C to stop viewing logs)"
echo "------------------------------------------------"

docker logs -f srt-app
