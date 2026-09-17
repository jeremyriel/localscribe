#!/usr/bin/env sh
# Local Scribe launcher for macOS.
# Double-clicking this file opens Terminal and runs it, so it resolves its own
# directory rather than relying on the working directory.
#
# Finds a supported Python, then hands over to bootstrap.py, which creates the
# virtual environment, installs dependencies, stops any running instance, and
# starts the server. bootstrap.py itself can find or download a supported
# Python once *some* interpreter is running it; when there is no Python at
# all on the machine, that trick has nothing to run bootstrap.py with, so
# this script downloads one directly first. See fetch_python() below.

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

# Downloads the same self-contained CPython build bootstrap.py downloads
# (python-build-standalone, the interpreters `uv` installs), so a machine
# with no Python at all can still run bootstrap.py. Cached under
# .pyruntime/, shared with bootstrap.py's own copy of this logic.
fetch_python() {
  command -v curl >/dev/null 2>&1 && command -v tar >/dev/null 2>&1 || return 1

  case "$(uname -m)" in
    x86_64) arch=x86_64 ;;
    arm64|aarch64) arch=aarch64 ;;
    *) return 1 ;;
  esac
  triple="${arch}-apple-darwin"

  json="$(curl -fsSL --max-time 20 \
    https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest 2>/dev/null)"
  [ -n "$json" ] || return 1

  url=""
  for pyver in 3.13 3.12 3.11; do
    url="$(printf '%s' "$json" | grep -o "\"browser_download_url\": *\"[^\"]*cpython-${pyver}\.[^\"]*-${triple}-install_only_stripped\.tar\.gz\"" \
      | head -n 1 | sed -E 's/.*"(https:[^"]+)"/\1/')"
    [ -n "$url" ] && break
  done
  [ -n "$url" ] || return 1

  # The URL percent-encodes "+" as "%2B"; undo that so the cache directory
  # matches the plain asset name bootstrap.py's own copy of this logic uses.
  name="$(basename "$url" | sed 's/%2B/+/g')"
  dest=".pyruntime/${name%.tar.gz}"
  python_bin="$dest/python/bin/python3"

  if [ ! -x "$python_bin" ]; then
    echo "  No Python found on this system. Downloading one ($pyver, ~30-60 MB)..."
    mkdir -p "$dest" || return 1
    tmp="$(mktemp)" || return 1
    if ! curl -fsSL --max-time 180 -o "$tmp" "$url"; then
      rm -f "$tmp"
      return 1
    fi
    tar -xzf "$tmp" -C "$dest"
    status=$?
    rm -f "$tmp"
    [ "$status" -eq 0 ] || return 1
  fi

  [ -x "$python_bin" ] && printf '%s' "$python_bin"
}

PYTHON="$(find_python)" || PYTHON="$(fetch_python)" || {
  echo
  echo "  Local Scribe could not start: no Python interpreter was found, and"
  echo "  one could not be downloaded automatically (no internet connection?)."
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
