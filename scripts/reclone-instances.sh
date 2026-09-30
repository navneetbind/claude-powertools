#!/bin/bash
# Keep every Claude instance (own profile, own notifications, own menu-bar icon)
# in step with /Applications/Claude.app.
#
#   ./reclone-instances.sh             rebuild only what needs it
#   ./reclone-instances.sh --force     rebuild every instance
#   ./reclone-instances.sh --dry-run   say what would happen, change nothing
#   ./reclone-instances.sh --add "Claude Work 7"   create a brand-new instance
#
# Each instance is TWO bundles:
#
#   REAL COPY   ~/Applications/Claude Instances/<Name>.app
#     A full copy of Claude.app, own bundle id + display name, re-signed ad-hoc.
#     Its main executable is the genuine Mach-O (NOT a shell shim), so macOS
#     sees a process whose signature matches its bundle. That is what lets it
#     register a menu-bar item and get notification permission. (Tested
#     2026-10-01: the old "rename the binary to Claude-real and put a shell
#     script in its place" layout signs the running binary as
#     "Claude-real-<hash>", Info.plist not bound - macOS then never listed it
#     under Menu Bar / gave it notifications.) Lives in your home folder, so
#     rebuilding it needs NO password.
#
#   LAUNCHER    /Applications/<Name>.app   (~1 MB, never changes across updates)
#     What you click / pin in the Dock. Starts the real copy with
#     --user-data-dir=<profile>, or just brings it to the front if already
#     running. The real copy is kept out of /Applications so it cannot be opened
#     by accident on the DEFAULT profile.
#
# Copies cannot auto-update (ad-hoc signature pins a cdhash), so this script
# rebuilds them after each Claude.app update - update-claude.sh calls it.
# Electron's asar-integrity fuse is on, so the profile path cannot be baked
# into the app; hence the launcher.

set -u
SRC="/Applications/Claude.app"
SHARED_ID="com.anthropic.claudefordesktop"
REALDIR="$HOME/Applications/Claude Instances"
FORCE=0; DRY=0; ADD=""
while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1 ;;
    --dry-run|-n) DRY=1 ;;
    --add) shift; ADD="${1:-}"; [ -n "$ADD" ] || { echo "--add needs a name" >&2; exit 2; } ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
if [ -n "$ADD" ]; then
  printf '%s' "$ADD" | grep -qE '^[A-Za-z0-9][A-Za-z0-9 _-]{0,48}$' \
    || { echo "name must be letters, numbers, spaces, - or _ (max 49)" >&2; exit 2; }
fi

PB=/usr/libexec/PlistBuddy
ME=$(id -un)
TRASH="$HOME/.Trash"
[ -d "$SRC" ] || { echo "$SRC not found." >&2; exit 1; }
SRCV=$($PB -c "Print :CFBundleShortVersionString" "$SRC/Contents/Info.plist")
echo "Claude.app is $SRCV"

slug() { printf '%s' "$1" | sed -E 's/[^A-Za-z0-9]+/-/g; s/^-+//; s/-+$//' | tr 'A-Z' 'a-z'; }
# Is any process running an executable from inside this bundle?
# chrome-native-host (Chrome-extension bridge) can outlive the app by hours and is
# harmless to kill, so it is cleared first and never counts as "running".
in_use() {
  pkill -f "$1/Contents/Helpers/chrome-native-host" 2>/dev/null
  ps -Axo comm= | awk -v p="$1/Contents/" 'index($0,p)==1 && index($0,"/Helpers/chrome-native-host")==0{n++} END{exit n?0:1}'
}
# Is this data folder open in any Claude process at all?
profile_open() {
  ps -Axww -o args= | awk -v d="--user-data-dir=$1" '
    { i=index($0,d); if(i){ c=substr($0,i+length(d),1); if(c==""||c==" ") n++ } }
    END{exit n?0:1}'
}

NAMES=(); DATAS=(); IDS=(); NEED_REAL=(); NEED_LAUNCH=(); WHY=()
for app in /Applications/*.app; do
  shim="$app/Contents/MacOS/Claude"
  [ -f "$shim" ] || continue
  [ "$(head -c 2 "$shim")" = "#!" ] || continue          # the real app is a Mach-O
  name=$(basename "$app" .app)
  case "$name" in *.old-*|*.staging|*" (replaced "*) continue ;; esac
  data=$(grep -oE "user-data-dir=(\"[^\"]+\"|'[^']+')" "$shim" | head -1 | sed -E 's/^user-data-dir=.//; s/.$//')
  [ -n "$data" ] || continue

  id="$SHARED_ID.$(slug "$name")"
  real="$REALDIR/$name.app"
  why=""; nr=0; nl=0
  # Anything that is not the current launcher (copy with a Claude-real shim, or
  # the older 1 MB launcher that execs Claude.app directly) gets converted.
  if ! grep -q 'open -n' "$shim"; then nl=1; why="old layout (will convert)"; fi
  if [ ! -d "$real" ]; then nr=1; why="${why:+$why, }no real copy yet"
  else
    v=$($PB -c "Print :CFBundleShortVersionString" "$real/Contents/Info.plist" 2>/dev/null)
    [ "$v" != "$SRCV" ] && { nr=1; why="${why:+$why, }copy on ${v:-?}"; }
  fi
  [ -z "$why" ] && [ "$FORCE" = 1 ] && { nr=1; why="--force"; }

  if [ -z "$why" ]; then
    echo "  ok       $name  ($id, $SRCV)"
  else
    echo "  rebuild  $name  - $why"
    NAMES+=("$name"); DATAS+=("$data"); IDS+=("$id"); NEED_REAL+=("$nr"); NEED_LAUNCH+=("$nl"); WHY+=("$why")
  fi
done

if [ -n "$ADD" ]; then
  if [ -d "/Applications/$ADD.app" ]; then
    echo "/Applications/$ADD.app already exists - pick another name, or run without --add to repair it." >&2; exit 1
  fi
  NAMES+=("$ADD")
  DATAS+=("$HOME/Library/Application Support/$(printf '%s' "$ADD" | sed -E 's/[^A-Za-z0-9]+/-/g; s/^-+//; s/-+$//')")
  IDS+=("$SHARED_ID.$(slug "$ADD")"); NEED_REAL+=(1); NEED_LAUNCH+=(1); WHY+=("new instance")
  echo "  create   $ADD  - new instance"
fi

if [ ${#NAMES[@]} -eq 0 ]; then
  echo "All instances current. Nothing to do."
  exit 0
fi
[ "$DRY" = 1 ] && { echo "(dry run - nothing changed)"; exit 0; }

# Nothing may be running from a bundle we are about to replace: Electron reads
# app.asar lazily, so a live window would break.
for i in "${!NAMES[@]}"; do
  for app in "$REALDIR/${NAMES[$i]}.app" "/Applications/${NAMES[$i]}.app"; do
    [ -d "$app" ] || continue
    [ "${NEED_REAL[$i]}" = 1 ] || [ "${NEED_LAUNCH[$i]}" = 1 ] || continue
    if in_use "$app"; then
      echo "==> Quitting ${NAMES[$i]}..."
      osascript -e "if application id \"${IDS[$i]}\" is running then tell application id \"${IDS[$i]}\" to quit" >/dev/null 2>&1 || true
      for _ in $(seq 1 40); do in_use "$app" || break; sleep 1; done
      in_use "$app" && { echo "  ${NAMES[$i]} is still running. Quit it (Cmd-Q) and re-run." >&2; exit 1; }
    fi
  done
done

TMP=$(mktemp -d /tmp/claude-reclone.XXXXXX)
trap 'rm -rf "$TMP"' EXIT
STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p "$REALDIR"

build_real() {   # name id -> $TMP/real/<name>.app
  local name="$1" id="$2" st="$TMP/real/$1.app"
  mkdir -p "$TMP/real"
  echo "==> Building $name ($SRCV, $id)..."
  # --noqtn: a quarantined ad-hoc copy is refused by Gatekeeper, and every
  # process launched from its window is SIGKILLed.
  ditto --noqtn "$SRC" "$st" || { echo "  copy failed" >&2; exit 1; }
  xattr -dr com.apple.quarantine "$st" 2>/dev/null || true
  # The id must be set BEFORE signing: Info.plist is part of the seal.
  $PB -c "Set :CFBundleIdentifier $id" "$st/Contents/Info.plist"
  # DisplayName ONLY - Electron finds "<CFBundleName> Helper.app" from
  # CFBundleName, so renaming that SIGTRAPs the main process at startup.
  $PB -c "Set :CFBundleDisplayName $name" "$st/Contents/Info.plist" 2>/dev/null \
    || $PB -c "Add :CFBundleDisplayName string $name" "$st/Contents/Info.plist"
  codesign --force --deep --sign - "$st" 2>/dev/null || { echo "  signing failed" >&2; exit 1; }
  codesign --verify --deep --strict "$st" 2>/dev/null || { echo "  signature does not verify" >&2; exit 1; }
  # Identity check: the running binary must be signed as the bundle id.
  codesign -dv "$st/Contents/MacOS/Claude" 2>&1 | grep -q "Identifier=$id" \
    || { echo "  main binary not signed as $id" >&2; exit 1; }
}

build_launcher() {   # name id data real -> $TMP/launch/<name>.app
  local name="$1" id="$2" data="$3" real="$4" st="$TMP/launch/$1.app"
  mkdir -p "$st/Contents/MacOS" "$st/Contents/Resources"
  local icon; icon=$($PB -c "Print :CFBundleIconFile" "$SRC/Contents/Info.plist" 2>/dev/null); icon="${icon%.icns}"
  cp "$SRC/Contents/Resources/$icon.icns" "$st/Contents/Resources/electron.icns" 2>/dev/null || true
  cat > "$st/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>Claude</string>
<key>CFBundleIdentifier</key><string>$id.launcher</string>
<key>CFBundleName</key><string>$name</string>
<key>CFBundleDisplayName</key><string>$name</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleIconFile</key><string>electron</string>
<key>CFBundleVersion</key><string>1</string>
<key>CFBundleShortVersionString</key><string>launcher</string>
<key>LSUIElement</key><true/>
</dict></plist>
PLIST
  cat > "$st/Contents/MacOS/Claude" <<SH
#!/bin/bash
# Launcher for the "$name" Claude instance. Starts the real copy on its own
# profile, or brings it to the front when it is already running.
# user-data-dir="$data"
DATA="$data"
REAL="$real"
ID="$id"
if pgrep -f -- "--user-data-dir=\$DATA\\\$" >/dev/null 2>&1; then
  exec /usr/bin/open -b "\$ID"
fi
exec /usr/bin/open -n "\$REAL" --args --user-data-dir="\$DATA"
SH
  chmod +x "$st/Contents/MacOS/Claude"
  codesign --force --sign - "$st" 2>/dev/null || { echo "  launcher signing failed" >&2; exit 1; }
}

LSREG=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
SWAP="$TMP/swap.sh"; { echo '#!/bin/bash'; echo 'set -e'; } > "$SWAP"
NEEDSWAP=0

for i in "${!NAMES[@]}"; do
  name="${NAMES[$i]}"; id="${IDS[$i]}"; data="${DATAS[$i]}"; real="$REALDIR/$name.app"
  if [ "${NEED_REAL[$i]}" = 1 ]; then
    build_real "$name" "$id"
    if [ -d "$real" ]; then mv "$real" "$TRASH/$name (replaced $STAMP).app"; fi
    mv "$TMP/real/$name.app" "$real"      # home folder: no password needed
    "$LSREG" -f "$real" 2>/dev/null
  fi
  if [ "${NEED_LAUNCH[$i]}" = 1 ]; then
    echo "==> Building launcher for $name..."
    build_launcher "$name" "$id" "$data" "$real"
    app="/Applications/$name.app"
    printf 'if [ -d %q ]; then chown -R %q:staff %q; mv %q %q; fi\n' \
      "$app" "$ME" "$app" "$app" "$TRASH/$name (old copy $STAMP).app" >> "$SWAP"
    printf 'chown -R root:admin %q\n' "$TMP/launch/$name.app" >> "$SWAP"
    printf 'mv %q %q\n' "$TMP/launch/$name.app" "$app" >> "$SWAP"
    NEEDSWAP=1
  fi
done

if [ "$NEEDSWAP" = 1 ]; then
  echo 'echo DONE' >> "$SWAP"; chmod +x "$SWAP"
  echo "==> Installing launchers (needs your password ONCE; /Applications is owned by root)..."
  OUT=$(osascript -e "do shell script \"$SWAP\" with administrator privileges" 2>&1)
  case "$OUT" in *DONE*) ;; *) echo "  Install failed: $OUT" >&2; exit 1 ;; esac
fi

FAIL=0; OPEN=()
for i in "${!NAMES[@]}"; do
  name="${NAMES[$i]}"; real="$REALDIR/$name.app"; app="/Applications/$name.app"
  "$LSREG" -f "$app" 2>/dev/null
  v=$($PB -c "Print :CFBundleShortVersionString" "$real/Contents/Info.plist" 2>/dev/null)
  id=$($PB -c "Print :CFBundleIdentifier" "$real/Contents/Info.plist" 2>/dev/null)
  if [ "$v" = "$SRCV" ] && [ "$id" = "${IDS[$i]}" ] && codesign --verify --deep --strict "$real" 2>/dev/null \
     && grep -q 'open -n' "$app/Contents/MacOS/Claude" 2>/dev/null; then
    echo "  done     $name  ($id, $v)"
  else
    echo "  FAILED   $name  (version '$v', id '$id')" >&2; FAIL=1
  fi
  profile_open "${DATAS[$i]}" && OPEN+=("$name")
done

cat <<'MSG'

First launch of each rebuilt instance:
  - macOS asks for "Claude Safe Storage". Click Always Allow. Until you do, the
    app sits with no window (the prompt can hide behind other windows).
  - System Settings > Notifications and > Menu Bar: each instance is its own
    row named after the instance. A new row may start OFF; turn it on.
  - Privacy > Full Disk Access: re-add the instance if you had granted it (its
    code identity changed). Real copies are in ~/Applications/Claude Instances.
Previous bundles are in your Trash.
MSG
if [ ${#OPEN[@]} -gt 0 ]; then
  echo
  echo "! Still open in a running window: ${OPEN[*]}"
  echo "  Quit that window before opening the rebuilt app."
fi
exit $FAIL
