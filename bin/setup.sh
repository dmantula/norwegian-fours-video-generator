#!/usr/bin/env bash
# Set up the local Python environment. Idempotent — safe to re-run.
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found. Install with 'brew install python'." >&2
  exit 1
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ERROR: ffmpeg not found. Install with 'brew install ffmpeg'." >&2
  exit 1
fi

if [[ ! -d .venv ]]; then
  echo "Creating .venv ..."
  python3 -m venv .venv
fi

.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

chmod +x bin/render.sh vidmerge.py 2>/dev/null || true

echo "Setup complete. Try: ./bin/render.sh config.yaml -o output.mp4 --test"
