#!/bin/zsh
# macOS launcher: double-click in Finder, or run from Terminal.
cd "${0:A:h}" || exit 1
PY=$(command -v python3 || command -v python)
if [[ -z "$PY" ]]; then
  print -u2 "Python 3.9 or newer is required. Install it with: brew install python"
  exit 2
fi
"$PY" scripts/menu.py "$@"
status=$?
[[ $status -ne 0 && -t 0 ]] && read -k 1 "?Press any key to close..."
exit $status
