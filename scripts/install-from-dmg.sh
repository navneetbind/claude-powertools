#!/bin/bash
# Install Claude from the verified DMG, bypassing ShipIt entirely.
# RUN FROM TERMINAL. It quits Claude itself; do not reopen Claude while it runs.
#
# Safety: the new app is copied in BESIDE the old one first. The old bundle is
# only moved aside once the copy is complete and verified, so a failure at any
# point leaves the working Claude.app in place.

set -u
DMG="${1:-$HOME/Downloads/Claude.dmg}"
REQ='anchor apple generic and identifier "com.anthropic.claudefordesktop" and certificate leaf[subject.OU] = Q6L2SF6YDW'

count() { ps -Ao args= | grep -c "^/Applications/Claude.app/Contents/MacOS/Claude "; }

echo "Current: $(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' /Applications/Claude.app/Contents/Info.plist 2>/dev/null)"

# --- mount ---
MNT=$(hdiutil attach "$DMG" -nobrowse -readonly 2>/dev/null | grep -o "/Volumes/.*" | head -1)
[ -z "$MNT" ] && { echo "Could not mount $DMG"; exit 1; }
SRC="$MNT/Claude.app"
cleanup() { hdiutil detach "$MNT" -quiet 2>/dev/null || true; }
trap cleanup EXIT

# --- verify BEFORE touching anything ---
echo "==> Verifying the DMG's app..."
codesign --verify --deep --strict "$SRC" 2>/dev/null || { echo "FAILED strict verify. Aborting."; exit 1; }
codesign --verify -R="$REQ" "$SRC" 2>/dev/null   || { echo "NOT a genuine Anthropic build. Aborting."; exit 1; }
spctl -a -t install "$SRC" 2>/dev/null           || { echo "Gatekeeper rejected it. Aborting."; exit 1; }
NEWV=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$SRC/Contents/Info.plist")
echo "    OK: genuine, notarized, version $NEWV"

# --- must be fully quit ---
echo "==> Quitting every Claude instance..."
osascript -e 'tell application "Claude" to quit' >/dev/null 2>&1 || true
for i in $(seq 1 40); do [ "$(count)" -eq 0 ] && break; sleep 1; done
if [ "$(count)" -ne 0 ]; then
  echo "    STILL RUNNING - close every Claude window (Cmd-Q) and re-run."
  exit 1
fi
echo "    All instances down."

# --- stage beside, then swap ---
STAMP=$(date +%Y%m%d-%H%M%S)
echo "==> Copying new app in (needs your password)..."
osascript -e "do shell script \"
  /usr/bin/ditto '$SRC' '/Applications/Claude.new.app' &&
  /bin/mv '/Applications/Claude.app' '/Applications/Claude.old-$STAMP.app' &&
  /bin/mv '/Applications/Claude.new.app' '/Applications/Claude.app'
\" with administrator privileges" 2>&1 | sed 's/^/    /'

# --- confirm ---
GOT=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" /Applications/Claude.app/Contents/Info.plist 2>/dev/null)
if [ "$GOT" = "$NEWV" ] && codesign --verify -R="$REQ" /Applications/Claude.app 2>/dev/null; then
  echo
  echo "SUCCESS: /Applications/Claude.app is now $GOT (verified genuine)"
  echo "Instances are rebuilt from it next."
  echo "Old bundle kept at /Applications/Claude.old-$STAMP.app - delete it once happy."
# Instances are full copies that cannot update themselves; bring them up to
# the Claude.app just installed.
if [ -x "$(dirname "$0")/reclone-instances.sh" ]; then
  echo; echo "==> Bringing instances up to Claude.app..."
  "$(dirname "$0")/reclone-instances.sh"
fi
  exit 0
fi

echo
echo "FAILED - version is '$GOT'. Nothing was destroyed."
echo "If the error above says 'Operation not permitted', macOS App Management blocked it."
echo "In that case do it in Finder instead: open $DMG and drag Claude.app onto"
echo "the Applications shortcut, choosing Replace. Finder is allowed to do this."
ls -d /Applications/Claude*.app 2>/dev/null
exit 1
