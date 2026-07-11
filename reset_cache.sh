#!/bin/bash
# Wipes the Streamio addon cache (translated + source subtitles) and restarts
# the addon container so the next request does a completely fresh lookup.
# Leaves the OpenSubtitles login session file intact so we don't burn a login.
set -euo pipefail

CACHE_DIR="$(pwd)/streamio_cache"
CONTAINER_NAME="srt-streamio"
DOCKER="docker"
docker info >/dev/null 2>&1 || DOCKER="sudo docker"

echo ">> Stopping addon container ($CONTAINER_NAME) so it releases the cache..."
$DOCKER stop "$CONTAINER_NAME" >/dev/null 2>&1 || true

if [[ -d "$CACHE_DIR" ]]; then
  echo ">> Cache before:"
  sudo find "$CACHE_DIR" -maxdepth 1 -type f | wc -l

  # Keep the OpenSubtitles session token; delete every cached subtitle.
  echo ">> Deleting cached subtitles (.srt, .google.srt, .source.srt, .meta.json)..."
  sudo find "$CACHE_DIR" -maxdepth 1 -type f \
    \( -name '*.srt' -o -name '*.google.srt' -o -name '*.source.srt' \
       -o -name '*.source.meta.json' \) -delete

  echo ">> Cache after (should only be the .opensubtitles_session.json, if any):"
  sudo ls -la "$CACHE_DIR" || true
else
  echo ">> No cache dir at $CACHE_DIR (nothing to clean)."
fi

echo ">> Restarting addon (no log tail)..."
NO_LOGS=1 sudo ./run_streamio.sh

echo "------------------------------------------------"
echo "Cache cleared and addon restarted."
echo "Now, on the TV: fully close the movie, reopen it, and pick the Hebrew sub."
echo "Watch selection live with:"
echo "  sudo docker logs -f $CONTAINER_NAME 2>&1 | grep -E 'search added|OST candidate|Using'"
echo "------------------------------------------------"
