#!/usr/bin/env sh
# Local Scribe launcher for Linux.
# Finds a supported Python, then hands over to bootstrap.py, which creates the
# virtual environment, installs dependencies, stops any running instance, and
# starts the server.

set -u

cd "$(dirname "$0")" || exit 1

find_python() {
  for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

PYTHON="$(find_python)" || {
  echo
  echo "  Local Scribe could not start: no Python interpreter was found."
  echo
  echo "  Install Python 3.13 (3.10 to 3.13 are supported), for example:"
  echo "    Debian/Ubuntu:  sudo apt install python3.13 python3.13-venv"
  echo "    Fedora:         sudo dnf install python3.13"
  echo "    Arch:           sudo pacman -S python"
  echo
  exit 1
}

exec "$PYTHON" bootstrap.py "$@"
