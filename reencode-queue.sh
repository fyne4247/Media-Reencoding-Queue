#!/usr/bin/env bash
# Linux launcher: run from a terminal.
cd "$(dirname "$(readlink -f "$0")")" || exit 1
PY=$(command -v python3 || command -v python)
if [ -z "$PY" ]; then
  echo "Python 3.9 or newer is required. Install it with your package manager (e.g. sudo apt install python3)." >&2
  exit 2
fi
exec "$PY" scripts/menu.py "$@"
