#!/bin/bash
# Run the translator CLI inside Docker (same image as the web server).
# Mounts the input file's directory as /data so output lands next to the input.
#
# Usage:
#   export GROQ_API_KEY=your_key
#   ./run_cli_docker.sh /path/to/movie.srt
#   ./run_cli_docker.sh /path/to/movie.srt -o /path/to/output.srt
#   ./run_cli_docker.sh /path/to/movie.srt --literal
#
# Use -o /data/name.srt to set output name (file appears next to input). Omit -o for default *_AI_POLISHED.srt.

set -e
IMAGE_NAME="${IMAGE_NAME:-hybrid-srt-translator}"
INPUT="${1:?Usage: $0 <input.srt> [ -o output.srt ] [ --literal ]}"
shift || true

INPUT_ABS="$(cd "$(dirname "$INPUT")" && pwd)/$(basename "$INPUT")"
INPUT_DIR="$(dirname "$INPUT_ABS")"
INPUT_FILE="$(basename "$INPUT_ABS")"

docker image inspect "$IMAGE_NAME" &>/dev/null || docker build -t "$IMAGE_NAME" .

echo "Running CLI in Docker (input: $INPUT_ABS)"
docker run --rm \
  -e GROQ_API_KEY="${GROQ_API_KEY}" \
  -v "$INPUT_DIR:/data:rw" \
  "$IMAGE_NAME" \
  python app.py translate "/data/$INPUT_FILE" "$@"

echo "Done. Output is in $INPUT_DIR"
