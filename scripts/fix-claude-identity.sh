#!/bin/bash
# One-shot cleanup for the multi-instance setup. Run from Terminal.app with
# EVERY Claude window quit (Cmd-Q) - not from inside a Claude session.
#
#  1. Strips the download quarantine from /Applications/Claude.app. While it is
#     set, macOS "translocates" the app to a random read-only path on every
#     launch: the updater cannot replace it, and notification/menu-bar identity
#     is a path that changes each time.
#  2. Moves the leftover  *.old-*.app  bundles out of /Applications. They carry
#     the SAME bundle id as the live apps, so LaunchServices can resolve a
#     notification click (or a status item) to a dead copy.
set -u
APP=/Applications/Claude.app
PB=/usr/libexec/PlistBuddy
die() { echo "ERROR: $*" >&2; exit 1; }

ps -Axo comm= | grep -q '/Applications/Claude.*\.app/Contents/MacOS/\|AppTranslocation.*/Claude.app/' \
  && die "A Claude instance is still running. Quit them all (Cmd-Q) and re-run."

codesign --verify --deep --strict "$APP" 2>/dev/null || die "$APP fails strict codesign. Not touching it."
codesign -dv "$APP" 2>&1 | grep -q 'TeamIdentifier=Q6L2SF6YDW' || die "$APP is not Anthropic-signed (Team ID mismatch)."
spctl -a -t exec "$APP" 2>/dev/null || die "Gatekeeper rejects $APP."

STAMP=$(date +%Y%m%d-%H%M%S)
PARK=/private/var/tmp/claude-old-$STAMP
NEEDQ=0; xattr "$APP" | grep -q com.apple.quarantine && NEEDQ=1
OLD=(); for o in /Applications/Claude*.old-*.app; do [ -d "$o" ] && OLD+=("$o"); done
echo "quarantined: $NEEDQ   leftover old bundles: ${#OLD[@]}"
[ "$NEEDQ" = 0 ] && [ ${#OLD[@]} -eq 0 ] && { echo "Nothing to do."; exit 0; }

SW=$(mktemp /tmp/fixid.XXXXXX); {
  echo '#!/bin/bash'; echo 'set -e'; printf 'mkdir -p %q\n' "$PARK"
  if [ "$NEEDQ" = 1 ]; then
    printf '/usr/bin/ditto --noqtn %q /Applications/Claude.new.app\n' "$APP"
    echo '/usr/bin/xattr -dr com.apple.quarantine /Applications/Claude.new.app'
    printf '/bin/mv %q %q\n' "$APP" "$PARK/Claude.quarantined.app"
    echo '/bin/mv /Applications/Claude.new.app /Applications/Claude.app'
  fi
  for o in "${OLD[@]}"; do printf '/bin/mv %q %q\n' "$o" "$PARK/"; done
  echo 'echo DONE'
} > "$SW"; chmod +x "$SW"
echo "==> Applying (needs your password)..."
OUT=$(osascript -e "do shell script \"$SW\" with administrator privileges" 2>&1)
rm -f "$SW"
case "$OUT" in *DONE*) ;; *) die "Failed: $OUT" ;; esac

LSREG=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
for a in "$PARK"/*.app; do [ -d "$a" ] && "$LSREG" -u "$a" 2>/dev/null; done
for a in /Applications/Claude*.app; do "$LSREG" -f "$a" 2>/dev/null; done
xattr "$APP" | grep -q com.apple.quarantine && echo "WARN: quarantine still set" || echo "quarantine: gone"
echo "Old bundles parked in $PARK (delete when happy)."
echo "Now relaunch each Claude and check Notifications + Menu Bar settings."
