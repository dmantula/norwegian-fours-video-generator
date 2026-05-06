#!/usr/bin/env bash
# Wrapper that runs vidmerge.py inside the project's .venv.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -x .venv/bin/python ]]; then
  echo "ERROR: .venv missing. Run ./bin/setup.sh first." >&2
  exit 1
fi

exec .venv/bin/python vidmerge.py "$@"
