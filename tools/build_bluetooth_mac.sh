#!/bin/bash
# Build the native architecture only. No install, account access or notarization.
set -euo pipefail
SOURCE="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${BEAMER_PYTHON:-$SOURCE/mac_app/.venv/bin/python3}"
if [ ! -x "$PYTHON" ]; then
    python3 -m venv "$SOURCE/mac_app/.venv"
fi
"$PYTHON" -m pip install -r "$SOURCE/mac_app/requirements-mac.txt"
cd "$SOURCE/mac_app"
"$PYTHON" setup.py py2app --arch="$(uname -m)"
APP="$SOURCE/mac_app/dist/Beamer Bluetooth.app"
# The application's own bundle identifier is separate from upstream Beamer.
codesign --force --deep --sign - --entitlements "$SOURCE/mac_app/entitlements.plist" "$APP"
echo "Built $APP"
