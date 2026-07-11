#!/bin/bash
# Stops ONLY the Streamio addon container. Leaves the local srt-app untouched.
set -euo pipefail
CONTAINER_NAME="srt-streamio"
DOCKER="docker"
docker info >/dev/null 2>&1 || DOCKER="sudo docker"
$DOCKER stop "$CONTAINER_NAME" 2>/dev/null || true
$DOCKER rm "$CONTAINER_NAME" 2>/dev/null || true
echo "Stopped and removed $CONTAINER_NAME (local srt-app untouched)."
