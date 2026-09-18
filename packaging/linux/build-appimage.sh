#!/usr/bin/env sh
# Builds a self-contained Local Scribe AppImage.
#
# Run from anywhere; paths below are resolved relative to this script, not
# the working directory. Output lands in dist/linux/. Requires curl and
# tar (both standard); appimagetool is fetched automatically if not
# already on PATH.
#
# What gets bundled: the app's own source (pure Python, small) and a
# self-contained CPython interpreter (python-build-standalone, the same
# builds bootstrap.py itself knows how to fetch at first run - here
# fetched once at build time and baked in instead). The heavy ASR packages
# (ctranslate2, ...) and the Whisper model itself are NOT bundled: they
# stay a one-time download on first launch, exactly as they already are
# for a from-source install - see the README's "one deliberate network
# request" privacy note. Baking them in would make this a multi-gigabyte
# download for no real benefit.
#
# No code signing applies to a plain downloaded Linux binary/AppImage -
# unlike macOS/Windows, there is no OS-level "unidentified developer"
# gate to get past here. Just chmod +x and run.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DIST="$ROOT/dist/linux"
APPDIR="$SCRIPT_DIR/build/LocalScribe.AppDir"
VERSION="$(cat "$ROOT/VERSION")"

ARCH="$(uname -m)"
case "$ARCH" in
  x86_64) TRIPLE="x86_64-unknown-linux-gnu"; AI_ARCH="x86_64" ;;
  aarch64) TRIPLE="aarch64-unknown-linux-gnu"; AI_ARCH="aarch64" ;;
  *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;;
esac

echo "==> Building Local Scribe AppImage ($VERSION, $TRIPLE)"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/app" "$DIST"

echo "==> Copying app source"
for item in app bootstrap.py requirements.txt VERSION LICENSE; do
  cp -R "$ROOT/$item" "$APPDIR/app/$item"
done

echo "==> Fetching a self-contained Python interpreter ($TRIPLE)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Authenticated when a token is available (CI sets GITHUB_TOKEN): GitHub
# Actions runners share a pool of outbound IPs that can exhaust the
# unauthenticated API rate limit fast; unset locally, this is simply a
# no-op extra header.
if [ -n "${GITHUB_TOKEN:-}" ]; then
  curl -fsSL --max-time 20 -H "Authorization: Bearer $GITHUB_TOKEN" \
    https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest \
    -o "$TMP/release.json"
else
  curl -fsSL --max-time 20 \
    https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest \
    -o "$TMP/release.json"
fi

PY_URL=""
for PYVER in 3.13 3.12 3.11; do
  PY_URL="$(python3 -c "
import json
with open('$TMP/release.json') as f:
    data = json.load(f)
for a in data['assets']:
    n = a['name']
    if n.startswith('cpython-$PYVER.') and '$TRIPLE' in n and 'freethreaded' not in n \
       and n.endswith('install_only_stripped.tar.gz'):
        print(a['browser_download_url'])
        break
")"
  [ -n "$PY_URL" ] && break
done
[ -n "$PY_URL" ] || { echo "Could not find a matching Python build for $TRIPLE" >&2; exit 1; }

curl -fsSL --max-time 180 -o "$TMP/python.tar.gz" "$PY_URL"
tar -xzf "$TMP/python.tar.gz" -C "$APPDIR/app"
mv "$APPDIR/app/python" "$APPDIR/python"

echo "==> Writing AppRun and desktop entry"
cat > "$APPDIR/AppRun" <<'APPRUN'
#!/usr/bin/env sh
HERE="$(cd "$(dirname "$0")" && pwd)"
export LOCALSCRIBE_DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/localscribe"
exec "$HERE/python/bin/python3" "$HERE/app/bootstrap.py" --desktop
APPRUN
chmod +x "$APPDIR/AppRun"

cat > "$APPDIR/localscribe.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Local Scribe
Comment=Offline transcription and transcript validation for research
Exec=AppRun --desktop
Icon=localscribe
Categories=Education;AudioVideo;
Terminal=false
DESKTOP

cp "$ROOT/assets/icon/icon-1024.png" "$APPDIR/localscribe.png"

echo "==> Fetching appimagetool"
APPIMAGETOOL="$TMP/appimagetool"
if command -v appimagetool >/dev/null 2>&1; then
  APPIMAGETOOL="$(command -v appimagetool)"
else
  curl -fsSL --max-time 60 -o "$APPIMAGETOOL" \
    "https://github.com/AppImage/appimagetool/releases/latest/download/appimagetool-${AI_ARCH}.AppImage"
  chmod +x "$APPIMAGETOOL"
fi

echo "==> Packaging AppImage"
OUT="$DIST/LocalScribe-$VERSION-Linux-$ARCH.AppImage"
rm -f "$OUT"
# CI runners typically have no FUSE available to mount the tool itself.
ARCH="$ARCH" "$APPIMAGETOOL" --appimage-extract-and-run "$APPDIR" "$OUT" \
  || ARCH="$ARCH" "$APPIMAGETOOL" "$APPDIR" "$OUT"
chmod +x "$OUT"

echo "==> Done: $OUT"
echo "    No signing needed on Linux - chmod +x (already set) and run."
