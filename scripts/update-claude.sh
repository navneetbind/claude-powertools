#!/bin/bash
# Update /Applications/Claude.app from Anthropic's own release feed.
# RUN FROM TERMINAL, and let it quit Claude itself.
#
# This is the same feed the app uses. From app.asar:
#   `${base}/api/desktop/${process.platform}/${arch}/squirrel/update?${params}`
# It answers with the version, a download URL on downloads.claude.ai, a sha256
# and a size. We check the sha256 AND Apple's signature before anything is
# installed, so a corrupted or substituted download cannot reach /Applications.
#
# Why not just let the app update itself: Squirrel refuses to install while any
# instance of the target app is running, allows ~4s for it to exit, and throws
# the download away on failure. See scripts/finish-update.sh.
#
# Afterwards it runs reclone-instances.sh, which rebuilds any instance (Claude
# Personal, Claude Work - 3, ...) that is now older than Claude.app. Instances
# are full copies with their own bundle id so notifications open the right
# window, and a copy cannot update itself. Run with nothing to update, this
# still brings stale instances up to Claude.app's version.

set -u
FEED_HOST="api.anthropic.com"
DL_HOST="downloads.claude.ai"
REQ='anchor apple generic and identifier "com.anthropic.claudefordesktop" and certificate leaf[subject.OU] = Q6L2SF6YDW'
APP="/Applications/Claude.app"
HERE="$(cd "$(dirname "$0")" && pwd)"

# Every Claude process: the real app, and each instance copy (Claude-real).
count() { ps -Axo comm= | grep -cE '^(/Users/[^/]+)?/Applications/([^/]+/)?[^/]+\.app/Contents/MacOS/Claude(-real)?$'; }
die() { echo "  $1" >&2; exit 1; }
reclone() {
  [ -x "$HERE/reclone-instances.sh" ] || return 0
  echo; echo "==> Bringing instances up to Claude.app..."
  "$HERE/reclone-instances.sh"
}
# Instances have their own bundle ids, so quitting "Claude" by name misses them.
quit_all() {
  local a id
  for a in /Applications/*.app "$HOME/Applications/Claude Instances"/*.app; do
    id=$(/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" "$a/Contents/Info.plist" 2>/dev/null)
    case "$id" in com.anthropic.claudefordesktop|com.anthropic.claudefordesktop.*)
      osascript -e "if application id \"$id\" is running then tell application id \"$id\" to quit" >/dev/null 2>&1 || true ;;
    esac
  done
}

[ -d "$APP" ] || die "$APP not found."
CUR=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$APP/Contents/Info.plist" 2>/dev/null)
echo "Installed: $CUR"

case "$(uname -m)" in arm64) ARCH=arm64 ;; *) ARCH=x64 ;; esac
# A throwaway id: the feed requires one, but nothing here should be traceable to
# this machine, and it is never written to disk.
DID=$(uuidgen | tr 'A-Z' 'a-z')

echo "==> Asking the release feed (darwin/$ARCH)..."
FEED="https://$FEED_HOST/api/desktop/darwin/$ARCH/squirrel/update?device_id=$DID&version=$CUR"
JSON=$(curl -fsS -m 30 "$FEED" 2>/dev/null) || {
  FEED="https://$FEED_HOST/api/desktop/darwin/universal/squirrel/update?device_id=$DID&version=$CUR"
  JSON=$(curl -fsS -m 30 "$FEED" 2>/dev/null) || die "Could not reach the update feed."
}

eval "$(printf '%s' "$JSON" | python3 -c '
import json, sys, urllib.parse, shlex
d = json.load(sys.stdin)
rels = d.get("releases") or []
if not rels:
    print("NEWV=; URL=; SHA=; SIZE=0"); sys.exit()
u = rels[0]["updateTo"]
host = urllib.parse.urlparse(u["url"]).hostname or ""
print("NEWV=" + shlex.quote(u.get("version","")))
print("URL="  + shlex.quote(u.get("url","")))
print("SHA="  + shlex.quote(u.get("sha256","")))
print("SIZE=" + shlex.quote(str(u.get("size",0))))
print("HOST=" + shlex.quote(host))
')"

[ -n "${NEWV:-}" ] || die "Feed returned no release."
echo "    Feed offers: $NEWV"

if [ "$NEWV" = "$CUR" ]; then
  echo
  echo "Claude.app already up to date on $CUR. Nothing downloaded."
  reclone
  exit $?
fi

# Refuse anything not served from Anthropic's own download host.
[ "${HOST:-}" = "$DL_HOST" ] || die "Refusing: download host is '${HOST:-}', expected $DL_HOST."
[ -n "${SHA:-}" ] || die "Refusing: feed gave no sha256."

TMP=$(mktemp -d /tmp/claude-upd.XXXXXX)
trap 'rm -rf "$TMP"' EXIT
PKG="$TMP/claude.zip"

echo "==> Downloading $NEWV ($(( ${SIZE:-0} / 1000000 )) MB) from $DL_HOST..."
curl -fL --progress-bar -m 1800 -o "$PKG" "$URL" || die "Download failed."

echo "==> Checking sha256..."
GOT=$(shasum -a 256 "$PKG" | awk '{print $1}')
[ "$GOT" = "$SHA" ] || die "CHECKSUM MISMATCH. Expected $SHA, got $GOT. Nothing installed."
echo "    matches feed"

echo "==> Unpacking and verifying the signature..."
ditto -x -k "$PKG" "$TMP/x" 2>/dev/null || die "Could not unpack."
SRC=$(find "$TMP/x" -maxdepth 2 -name "Claude.app" -type d | head -1)
[ -n "$SRC" ] || die "No Claude.app inside the download."
codesign --verify --deep --strict "$SRC" 2>/dev/null || die "Failed strict codesign. Nothing installed."
codesign --verify -R="$REQ" "$SRC" 2>/dev/null   || die "Not a genuine Anthropic build. Nothing installed."
spctl -a -t exec "$SRC" 2>/dev/null              || die "Gatekeeper rejected it. Nothing installed."
echo "    genuine, notarized, Team ID Q6L2SF6YDW"

echo "==> Quitting every Claude instance..."
for _ in 1 2 3; do quit_all; sleep 2; [ "$(count)" -eq 0 ] && break; done
for _ in $(seq 1 40); do [ "$(count)" -eq 0 ] && break; sleep 1; done
[ "$(count)" -eq 0 ] || die "Claude is still running. Close every window (Cmd-Q) and re-run."
echo "    all down"

STAMP=$(date +%Y%m%d-%H%M%S)
echo "==> Installing (needs your password; $APP is owned by root)..."
osascript -e "do shell script \"
  /usr/bin/ditto --noqtn '$SRC' '/Applications/Claude.new.app' && /usr/bin/xattr -dr com.apple.quarantine '/Applications/Claude.new.app' &&
  /bin/mv '$APP' '/Applications/Claude.old-$STAMP.app' &&
  /bin/mv '/Applications/Claude.new.app' '$APP'
\" with administrator privileges" >/dev/null 2>&1 || die "Install failed. $APP untouched."

GOT=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$APP/Contents/Info.plist" 2>/dev/null)
if [ "$GOT" = "$NEWV" ] && codesign --verify -R="$REQ" "$APP" 2>/dev/null; then
  echo
  echo "SUCCESS: $CUR -> $GOT (verified genuine)"
  echo "Previous version kept at /Applications/Claude.old-$STAMP.app"
  reclone
  exit $?
fi
die "Installed version reads '$GOT'. Check /Applications/Claude.old-$STAMP.app"
