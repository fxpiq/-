#!/usr/bin/env bash
set -euo pipefail

echo "[1/6] Python"
python --version

echo "[2/6] FFmpeg"
ffmpeg -hide_banner -version | head -n 1

echo "[3/6] yt-dlp"
yt-dlp --version

echo "[4/6] Faster-Whisper"
python -c "import faster_whisper; print('faster-whisper import: OK')"

echo "[5/6] FFmpeg encode"
ffmpeg -hide_banner -loglevel error \
  -f lavfi -i "testsrc2=size=640x360:rate=25" \
  -f lavfi -i "sine=frequency=880:sample_rate=48000" \
  -t 1 -c:v libx264 -pix_fmt yuv420p -c:a aac -f null -
echo "FFmpeg encode: OK"

echo "[6/6] Data directory"
test -w /app/data
python -c "from app.config import settings; print('data:', settings.data_dir)"
echo "Runtime check: OK"

