#!/bin/bash
# Rebuild every Claude instance as a full, independently-signed copy of
# /Applications/Claude.app with its own bundle id, keeping its data folder.
#
#   ./reclone-instances.sh             rebuild only what needs it
#   ./reclone-instances.sh --force     rebuild every instance
#   ./reclone-instances.sh --dry-run   say what would happen, change nothing
#
# Why copies rather than 1 MB launchers: a notification click activates an app
# by bundle id. A launcher execs the real Claude.app, so every instance *is*
# com.anthropic.claudefordesktop and every click lands in the default profile.
# A copy carries its own id, so the click opens the window that sent it.
#
# The price: changing the id means re-signing, and an ad-hoc signature pins the
# copy to its own cdhash, so it can never validate a genuine update itself. So
# copies are rebuilt from Claude.app after every update - update-claude.sh runs
# this at the end. Its frameworks cannot be shared with Claude.app by symlink or
# hard link: the hardened runtime SIGKILLs an ad-hoc binary that loads
# Anthropic-signed frameworks, and re-signing a hard link would rewrite
# Claude.app's own files.
#
# An instance needs rebuilding when it is a launcher, when it shares the real
# app's bundle id, or when its version differs from Claude.app.

set -u
SRC="/Applications/Claude.app"
SHARED_ID="com.anthropic.claudefordesktop"
FORCE=0; DRY=0
for a in "$@"; do
  case "$a" in
    --force) FORCE=1 ;;
    --dry-run|-n) DRY=1 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

PB=/usr/libexec/PlistBuddy
ME=$(id -un)
TRASH="$HOME/.Trash"
[ -d "$SRC" ] || { echo "$SRC not found." >&2; exit 1; }
SRCV=$($PB -c "Print :CFBundleShortVersionString" "$SRC/Contents/Info.plist")
echo "Claude.app is $SRCV"

slug() { printf '%s' "$1" | sed -E 's/[^A-Za-z0-9]+/-/g; s/^-+//; s/-+$//' | tr 'A-Z' 'a-z'; }
# Is any process running an executable from inside this bundle?
in_use() { ps -Axo comm= | awk -v p="$1/Contents/MacOS/" 'index($0,p)==1{n++} END{exit n?0:1}'; }
# Is this data folder open in any Claude process at all (launcher, copy or helper)?
profile_open() {
  ps -Axww -o args= | awk -v d="--user-data-dir=$1" '
    { i=index($0,d); if(i){ c=substr($0,i+length(d),1); if(c==""||c==" ") n++ } }
    END{exit n?0:1}'
}

NAMES=(); DATAS=(); IDS=(); WHY=()
for app in /Applications/*.app; do
  shim="$app/Contents/MacOS/Claude"
  [ -f "$shim" ] || continue
  [ "$(head -c 2 "$shim")" = "#!" ] || continue          # the real app is a Mach-O
  name=$(basename "$app" .app)
  case "$name" in *.old-*|*.staging|*" (replaced "*) continue ;; esac
  data=$(grep -oE "user-data-dir=(\"[^\"]+\"|'[^']+')" "$shim" | head -1 | sed -E 's/^user-data-dir=.//; s/.$//')
  [ -n "$data" ] || continue

  id=$($PB -c "Print :CFBundleIdentifier" "$app/Contents/Info.plist" 2>/dev/null)
  case "$id" in "$SHARED_ID."?*) ;; *) id="$SHARED_ID.$(slug "$name")" ;; esac

  why=""
  if grep -q "Claude-real" "$shim"; then
    v=$($PB -c "Print :CFBundleShortVersionString" "$app/Contents/Info.plist" 2>/dev/null)
    have=$($PB -c "Print :CFBundleIdentifier" "$app/Contents/Info.plist" 2>/dev/null)
    [ "$v" != "$SRCV" ] && why="copy on $v"
    [ "$have" = "$SHARED_ID" ] && why="shares the real app's bundle id"
  else
    why="launcher (notifications open the default profile)"
  fi
  [ -z "$why" ] && [ "$FORCE" = 1 ] && why="--force"

  if [ -z "$why" ]; then
    echo "  ok       $name  ($id, $SRCV)"
  else
    echo "  rebuild  $name  - $why"
    NAMES+=("$name"); DATAS+=("$data"); IDS+=("$id"); WHY+=("$why")
  fi
done

if [ ${#NAMES[@]} -eq 0 ]; then
  echo "All instances current. Nothing to do."
  exit 0
fi
[ "$DRY" = 1 ] && { echo "(dry run - nothing changed)"; exit 0; }

# A copy's bundle must not be replaced under a running process: Electron reads
# app.asar lazily, so the running window would break. Launchers are never in
# use - their process runs from Claude.app - so they need no quitting.
for i in "${!NAMES[@]}"; do
  app="/Applications/${NAMES[$i]}.app"
  if in_use "$app"; then
    echo "==> Quitting ${NAMES[$i]}..."
    osascript -e "if application id \"${IDS[$i]}\" is running then tell application id \"${IDS[$i]}\" to quit" >/dev/null 2>&1 || true
    for _ in $(seq 1 40); do in_use "$app" || break; sleep 1; done
    in_use "$app" && { echo "  ${NAMES[$i]} is still running. Quit it (Cmd-Q) and re-run." >&2; exit 1; }
  fi
done

TMP=$(mktemp -d /tmp/claude-reclone.XXXXXX)
trap 'rm -rf "$TMP"' EXIT

for i in "${!NAMES[@]}"; do
  name="${NAMES[$i]}"; st="$TMP/$name.app"
  echo "==> Building $name ($SRCV, ${IDS[$i]})..."
  # --noqtn: a Claude.app installed from a browser-downloaded DMG carries the
  # quarantine flag. That is fine on the notarized original, but a quarantined
  # ad-hoc copy is refused by Gatekeeper ("Apple could not verify ...") - and
  # every process launched from a window of it is SIGKILLed.
  ditto --noqtn "$SRC" "$st" || { echo "  copy failed" >&2; exit 1; }
  xattr -dr com.apple.quarantine "$st" 2>/dev/null || true
  mv "$st/Contents/MacOS/Claude" "$st/Contents/MacOS/Claude-real"
  {
    printf '%s\n' '#!/bin/bash'
    printf '%s\n' '# Claude instance: a full copy of Claude.app with its own bundle id, so a'
    printf '%s\n' '# notification click opens this window. Rebuilt by reclone-instances.sh.'
    printf '%s\n' 'DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"'
    printf 'exec "$DIR/Claude-real" --user-data-dir="%s" "$@"\n' "${DATAS[$i]}"
  } > "$st/Contents/MacOS/Claude"
  chmod +x "$st/Contents/MacOS/Claude"
  # The id must be set BEFORE signing: Info.plist is part of the seal.
  $PB -c "Set :CFBundleIdentifier ${IDS[$i]}" "$st/Contents/Info.plist"
  codesign --force --deep --sign - "$st" 2>/dev/null || { echo "  signing failed" >&2; exit 1; }
  codesign --verify --deep --strict "$st" 2>/dev/null || { echo "  signature does not verify" >&2; exit 1; }
done

# One password prompt for the lot. Old bundles go to your Trash, re-owned to you
# so emptying it does not ask for a password.
STAMP=$(date +%Y%m%d-%H%M%S)
SWAP="$TMP/swap.sh"
{
  echo '#!/bin/bash'
  echo 'set -e'
  for name in "${NAMES[@]}"; do
    app="/Applications/$name.app"
    printf 'if [ -d %q ]; then chown -R %q:staff %q; mv %q %q; fi\n' \
      "$app" "$ME" "$app" "$app" "$TRASH/$name (replaced $STAMP).app"
    printf 'chown -R root:admin %q\n' "$TMP/$name.app"
    printf 'mv %q %q\n' "$TMP/$name.app" "$app"
  done
  echo 'echo DONE'
} > "$SWAP"
chmod +x "$SWAP"

echo "==> Installing (needs your password; /Applications is owned by root)..."
OUT=$(osascript -e "do shell script \"$SWAP\" with administrator privileges" 2>&1)
case "$OUT" in *DONE*) ;; *) echo "  Install failed: $OUT" >&2; exit 1 ;; esac

LSREG=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
FAIL=0; OPEN=()
for i in "${!NAMES[@]}"; do
  app="/Applications/${NAMES[$i]}.app"
  "$LSREG" -f "$app" 2>/dev/null
  v=$($PB -c "Print :CFBundleShortVersionString" "$app/Contents/Info.plist" 2>/dev/null)
  id=$($PB -c "Print :CFBundleIdentifier" "$app/Contents/Info.plist" 2>/dev/null)
  if [ "$v" = "$SRCV" ] && [ "$id" = "${IDS[$i]}" ] && codesign --verify --deep --strict "$app" 2>/dev/null; then
    echo "  done     ${NAMES[$i]}  ($id, $v)"
  else
    echo "  FAILED   ${NAMES[$i]}  (version '$v', id '$id')" >&2; FAIL=1
  fi
  profile_open "${DATAS[$i]}" && OPEN+=("${NAMES[$i]}")
done

cat <<'MSG'

First launch of each rebuilt instance:
  - macOS asks for "Claude Safe Storage". Click Always Allow. Until you do, the
    app sits with no window (the prompt can hide behind other windows).
  - Notifications: System Settings > Notifications. Each instance is its own
    row, all named "Claude". A new row starts OFF; turn it on.
Previous bundles are in your Trash.
MSG
if [ ${#OPEN[@]} -gt 0 ]; then
  echo
  echo "! Still open in a running window: ${OPEN[*]}"
  echo "  Quit that window before opening the rebuilt app, or two processes"
  echo "  will share one data folder."
fi
exit $FAIL
