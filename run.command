#!/usr/bin/env sh
# Local Scribe launcher for macOS.
# Double-clicking this file opens Terminal and runs it, so it resolves its own
# directory rather than relying on the working directory.

set -u

cd "$(dirname "$0")" || exit 1

find_python() {
  for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  # Homebrew installs that are not on a double-clicked shell's PATH.
  for candidate in /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 \
                   /opt/homebrew/bin/python3 /usr/local/bin/python3.13 \
                   /usr/local/bin/python3 /usr/bin/python3; do
    if [ -x "$candidate" ]; then
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
  echo "  Install Python 3.13 (3.10 to 3.13 are supported) from"
  echo "  https://www.python.org/downloads/macos/ or with Homebrew:"
  echo "    brew install python@3.13"
  echo
  echo "  Press Enter to close this window."
  read -r _
  exit 1
}

"$PYTHON" bootstrap.py "$@"
STATUS=$?

# Keep the Terminal window readable if the launcher exited with an error.
if [ "$STATUS" -ne 0 ]; then
  echo
  echo "  Local Scribe exited with status $STATUS."
  echo "  Press Enter to close this window."
  read -r _
fi

exit "$STATUS"
