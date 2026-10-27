#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_SOURCE="$SCRIPT_DIR/dist/Beamer Bluetooth.app"
DEST_APP="/Applications/Beamer Bluetooth.app"

trap 'status=$?; echo "Installation failed."; read -r -p "Press Return to close"; exit $status' ERR

bash "$SCRIPT_DIR/../tools/build_bluetooth_mac.sh"
killall "Beamer Bluetooth" 2>/dev/null || true

rm -rf "$DEST_APP"
ditto "$APP_SOURCE" "$DEST_APP"
xattr -cr "$DEST_APP"

# An ad-hoc build is a different code identity on every build wearing the same bundle id. macOS
# keeps the old grant against the old identity, which is why such a reinstall can sit in the
# Privacy pane with a tick beside it and still receive no events -- the tick belongs to a build
# that no longer exists. Revoking turns that silent failure into a visible prompt.
# Only ad-hoc builds need it. build_dmg.sh signs with a real identity where one
# is reachable, and that identity is stable across rebuilds, so the grants carry over and
# revoking them would throw away the very thing the signing bought. The ad-hoc path still
# exists for builds driven over SSH, where the login keychain cannot be unlocked.
# Resetting resolves the id through LaunchServices, so it runs after the new bundle is in place,
# and never blocks the install: a first install on a clean machine has nothing to revoke.
BUNDLE_ID="local.beamer.bluetooth"
if codesign -dv "$DEST_APP" 2>&1 | grep -q "Signature=adhoc"; then
    for service in Accessibility ListenEvent LocalNetwork; do
        if tccutil reset "$service" "$BUNDLE_ID" >/dev/null 2>&1; then
            echo "Revoked $service"
        else
            echo "Nothing to revoke for $service"
        fi
    done
    REVOKED=1
else
    REVOKED=0
fi

# The app is installed, so the staged copy has done its job and must not survive. Spotlight
# indexes any .app left under build/ or dist/, which puts a second Beamer in Launchpad beside the
# real one with no way to tell them apart. build_dmg.sh stages inside the repo, so nothing else
# clears these.
rm -rf "$SCRIPT_DIR/build" "$SCRIPT_DIR/dist"

echo "Beamer is installed at $DEST_APP"
if [ "$REVOKED" -eq 1 ]; then
    echo "This build is ad-hoc signed, so its privacy permissions were revoked on install. Grant Accessibility and Input Monitoring again, one at a time, inside the app."
else
    echo "Signed with a stable identity, so Accessibility and Input Monitoring carry over. Grant them inside the app only if it asks."
fi
read -r -p "Press Return to close" || true
