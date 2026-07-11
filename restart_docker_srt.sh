#!/bin/bash

echo "Stopping old container..."
sudo docker stop srt-app 2>/dev/null
sudo docker rm srt-app 2>/dev/null

# Uncomment the line below to force a full rebuild so code changes take effect
# sudo docker build --no-cache -t hybrid-srt-translator .
sudo docker build -t hybrid-srt-translator .

echo "Launching Groq Hybrid Server..."
./run.sh
