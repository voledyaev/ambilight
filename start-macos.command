#!/bin/bash
# Double-clickable launcher for ambilight on macOS.
# Finder/double-click runs this in Terminal. We cd into the project, make
# sure uv is reachable (Finder starts with a minimal PATH), then run it.
cd "$(dirname "$0")" || exit 1

# uv installs to one of these depending on the install method; add them all.
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
  echo
  echo "  ERROR: 'uv' was not found on this system."
  echo "  Install it from https://docs.astral.sh/uv/  then run this again."
  echo
  read -r -p "Press Enter to close..."
  exit 1
fi

echo "Starting ambilight...  (close this window or press Ctrl+C to stop)"
echo
uv run ambilight "$@"

echo
echo "ambilight stopped."
read -r -p "Press Enter to close..."
