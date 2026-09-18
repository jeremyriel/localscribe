#!/usr/bin/env sh
# Builds Local Scribe.app and wraps it in a .dmg for distribution.
#
# Run from anywhere; paths below are resolved relative to this script, not
# the working directory. Output lands in dist/macos/.
#
# What gets bundled: the app's own source (pure Python, small) and a
# self-contained CPython interpreter (python-build-standalone, the same
# builds bootstrap.py itself knows how to fetch at first run - here fetched
# once at build time and baked in instead). The heavy ASR packages
# (ctranslate2, mlx-whisper, ...) and the Whisper model itself are NOT
# bundled: they stay a one-time download on first launch, exactly as they
# already are for a from-source install - see bootstrap.py's own docstring
# and the README's "one deliberate network request" privacy note. Baking
# them in would make this a multi-gigabyte download for no real benefit.
#
# Unsigned by default - macOS will show the Gatekeeper "unidentified
# developer" prompt on first open (documented in the README). Signing with
# a paid Apple Developer ID + notarization can be added as an additional
# step below later without changing anything else here.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DIST="$ROOT/dist/macos"
APP="$DIST/Local Scribe.app"
VERSION="$(cat "$ROOT/VERSION")"

ARCH="$(uname -m)"
case "$ARCH" in
  arm64) TRIPLE="aarch64-apple-darwin" ;;
  x86_64) TRIPLE="x86_64-apple-darwin" ;;
  *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;;
esac

echo "==> Building Local Scribe.app ($VERSION, $TRIPLE)"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/app"

echo "==> Copying app source"
# Only what's needed at runtime - not tests/, packaging/, .git, dist/, or
# any local state a prior dev run left behind (.venv, models, projects,
# logs, settings.json all get recreated fresh under the data directory).
for item in app bootstrap.py requirements.txt VERSION LICENSE; do
  cp -R "$ROOT/$item" "$APP/Contents/Resources/app/$item"
done

echo "==> Fetching a self-contained Python interpreter ($TRIPLE)"
PY_TMP="$(mktemp -d)"
trap 'rm -rf "$PY_TMP"' EXIT

RELEASE_JSON="$PY_TMP/release.json"
# Authenticated when a token is available (CI sets GITHUB_TOKEN): GitHub
# Actions runners share a pool of outbound IPs that can exhaust the
# unauthenticated API rate limit fast; unset locally, this is simply a
# no-op extra header.
if [ -n "${GITHUB_TOKEN:-}" ]; then
  curl -fsSL --max-time 20 -H "Authorization: Bearer $GITHUB_TOKEN" \
    https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest \
    -o "$RELEASE_JSON"
else
  curl -fsSL --max-time 20 \
    https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest \
    -o "$RELEASE_JSON"
fi

PY_URL=""
for PYVER in 3.13 3.12 3.11; do
  PY_URL="$(python3 -c "
import json
with open('$RELEASE_JSON') as f:
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

curl -fsSL --max-time 180 -o "$PY_TMP/python.tar.gz" "$PY_URL"
tar -xzf "$PY_TMP/python.tar.gz" -C "$APP/Contents/Resources/app"
# python-build-standalone extracts to a top-level "python/" directory.
mv "$APP/Contents/Resources/app/python" "$APP/Contents/Resources/python"

echo "==> Writing Info.plist"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Local Scribe</string>
  <key>CFBundleDisplayName</key><string>Local Scribe</string>
  <key>CFBundleIdentifier</key><string>org.trailblazerlab.localscribe</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>LocalScribe</string>
  <key>CFBundleIconFile</key><string>icon.icns</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSApplicationCategoryType</key><string>public.app-category.education</string>
</dict>
</plist>
PLIST

cp "$ROOT/assets/icon/icon.icns" "$APP/Contents/Resources/icon.icns"

echo "==> Writing launcher"
cat > "$APP/Contents/MacOS/LocalScribe" <<'LAUNCHER'
#!/usr/bin/env sh
# Resolves everything relative to the .app bundle's own location, so this
# works wherever the app has been moved to (Applications, Desktop, ...).
HERE="$(cd "$(dirname "$0")" && pwd)"
RESOURCES="$HERE/../Resources"
export LOCALSCRIBE_DATA_DIR="$HOME/Library/Application Support/Local Scribe"
exec "$RESOURCES/python/bin/python3" "$RESOURCES/app/bootstrap.py" --desktop
LAUNCHER
chmod +x "$APP/Contents/MacOS/LocalScribe"

echo "==> Packaging .dmg"
DMG="$DIST/LocalScribe-$VERSION-macOS-$ARCH.dmg"
rm -f "$DMG"
hdiutil create -volname "Local Scribe" -srcfolder "$APP" -ov -format UDZO "$DMG"

echo "==> Done: $DMG"
echo "    Unsigned - opening it for the first time needs the Gatekeeper"
echo "    right-click-Open workaround documented in the README until"
echo "    Developer ID signing + notarization is set up."
